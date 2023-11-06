import argparse
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from abc import ABC, abstractmethod
from argparse import ArgumentParser, _SubParsersAction
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import botocore.exceptions as boto_exceptions

import boto3
import google.cloud.storage as gcs
import wandb
from botocore.config import Config
from google.api_core.exceptions import NotFound

from olmo import TrainConfig

log = logging.getLogger(__name__)


R2_ACCOUNT_ID: str = "a198dc34621661a1a66a02d6eb7c4dc3"
CONFIG_YAML: str = "config.yaml"
WANDB_ENTITY: str = "ai2-llm"
DEFAULT_MAX_ARCHIVE_SIZE: float = 5_000_000_000  # 5GB


class CleaningOperations(Enum):
    DELETE_BAD_RUNS = auto()
    RENAME_RUNS_TO_WANDB_ID = auto()
    UNSHARD_CHECKPOINTS = auto()


class StorageType(Enum):
    LOCAL_FS = auto()
    GCS = auto()
    S3 = auto()
    R2 = auto()


class StorageAdapter(ABC):
    @abstractmethod
    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        """Lists all the entries within the directory or compressed file at the given path.
        Returns only top-level entries (i.e. not entries in subdirectories).

        max_file_size sets a threshold for the largest size file to retain within entries.
        Any file of larger size is not included in the returned results.
        """

    @abstractmethod
    def list_dirs(self, path: str) -> List[str]:
        """Lists all the directories within the directory or compressed file at the given path.
        Returns only top-level entries (i.e. not entries in subdirectories).
        """

    @abstractmethod
    def delete_path(self, path: str):
        """Deletes the entry at the given path and, if the path is a directory, delete all entries
        within its subdirectories.
        """

    @abstractmethod
    def is_file(self, path: str) -> bool:
        """Returns whether the given path corresponds to an existing file.
        """

    @abstractmethod
    def is_dir(self, path: str) -> bool:
        """Returns whether the given path corresponds to an existing directory.
        """

    @abstractmethod
    def download_to_folder(self, path: str, local_dest_folder: str):
        """Downloads the content from the directory or file at the path to the local FS destination folder.
        """

    @abstractmethod
    def upload(self, path: str, local_src: str):
        """Uploads the content from the directory or file at the local FS source to the path.
        """

    @classmethod
    def create_storage_adapter(cls, storage_type: StorageType, r2_account_id: Optional[str] = None):
        if storage_type == StorageType.LOCAL_FS:
            return LocalFileSystemAdapter()
        if storage_type == StorageType.GCS:
            return GoogleCloudStorageAdapter()
        if storage_type == StorageType.S3:
            return S3StorageAdapter()
        if storage_type == StorageType.R2:
            if r2_account_id is None:
                raise ValueError("R2 account id must be provided to create R2 storage adapter")
            return S3StorageAdapter(endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com")

        raise NotImplementedError(f"No storage adapter implemented for storage type {storage_type}")

    @staticmethod
    def _is_url(path: str) -> bool:
        return re.match(r"[a-z0-9]+://.*", str(path)) is not None

    @staticmethod
    def get_storage_type_for_path(path: str) -> StorageType:
        if StorageAdapter._is_url(path):
            parsed = urlparse(str(path))
            if parsed.scheme == "gs":
                return StorageType.GCS
            elif parsed.scheme == "s3":
                return StorageType.S3
            elif parsed.scheme == "r2":
                return StorageType.R2
            elif parsed.scheme == "file":
                path = path.replace("file://", "", 1)
                return StorageType.LOCAL_FS

        return StorageType.LOCAL_FS


class LocalFileSystemAdapter(StorageAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._temp_files: List[tempfile._TemporaryFileWrapper[bytes]] = []
        self._temp_dirs: List[tempfile.TemporaryDirectory] = []
        self._archive_extensions: List[str] = []

    def __del__(self):
        for temp_file in self._temp_files:
            temp_file.close()
        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def create_temp_file(self, suffix: Optional[str] = None) -> str:
        temp_file = tempfile.NamedTemporaryFile(suffix=suffix)
        self._temp_files.append(temp_file)
        return temp_file.name

    def create_temp_dir(self, suffix: Optional[str] = None) -> str:
        temp_dir = tempfile.TemporaryDirectory(suffix=suffix)
        self._temp_dirs.append(temp_dir)
        return temp_dir.name

    def has_supported_archive_extension(self, path: str) -> bool:
        if len(self._archive_extensions) == 0:
            self._archive_extensions = [
                extension.lower() for _, extensions, _ in shutil.get_unpack_formats() for extension in extensions
            ]

        return any(path.lower().endswith(extension) for extension in self._archive_extensions)

    def _list_entries(self, path: str, no_files: bool = False, max_file_size: Optional[float] = None) -> List[str]:
        if os.path.isdir(path):
            return [
                entry
                for entry in os.listdir(path)
                if ((not no_files or not os.path.isfile(os.path.join(path, entry)))
                    and (max_file_size is None or os.path.getsize(os.path.join(path, entry)) <= max_file_size))
            ]

        if self.has_supported_archive_extension(path):
            if no_files or max_file_size is not None:
                raise NotImplementedError("Filtering out entries from a tar file is not yet supported")

            with tarfile.open(path) as tar:
                tar_subpaths = [os.path.normpath(name) for name in tar.getnames()]
                return [
                    os.path.basename(tar_subpath) for tar_subpath in tar_subpaths if tar_subpath.count(os.sep) == 1
                ]

        raise ValueError(f"Path does not correspond to directory or supported archive file: {path}")

    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        return self._list_entries(path, max_file_size=max_file_size)

    def list_dirs(self, path: str) -> List[str]:
        return self._list_entries(path, no_files=True)

    def delete_path(self, path: str):
        path_obj = Path(path)
        if not path_obj.exists():
            return

        if path_obj.is_file():
            path_obj.unlink()
        else:
            shutil.rmtree(path)

    def is_file(self, path: str) -> bool:
        path_obj = Path(path)
        if not path_obj.exists():
            return False

        return path_obj.is_file()

    def is_dir(self, path: str) -> bool:
        path_obj = Path(path)
        if not path_obj.exists():
            return False

        return path_obj.is_dir()

    def download_to_folder(self, path: str, local_dest_folder: str):
        path_obj = Path(path)
        if not path_obj.exists():
            raise ValueError(f"No entry exists at path {path}")

        if path_obj.is_dir():
            shutil.copytree(path, local_dest_folder, dirs_exist_ok=True)
        elif path_obj.is_file():
            shutil.copy(path, local_dest_folder)
        else:
            raise RuntimeError(f"Unexpected type of path {path}")

    def upload(self, path: str, local_src: str):
        self.download_to_folder(local_src, path)


class GoogleCloudStorageAdapter(StorageAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._local_fs_adapter: Optional[LocalFileSystemAdapter] = None
        self._gcs_client: Optional[gcs.Client] = None
        self._temp_dirs: List[tempfile.TemporaryDirectory] = []

    @property
    def local_fs_adapter(self):
        if self._local_fs_adapter is None:
            self._local_fs_adapter = LocalFileSystemAdapter()

        return self._local_fs_adapter

    @property
    def gcs_client(self):
        if self._gcs_client is None:
            self._gcs_client = gcs.Client()

        return self._gcs_client

    @staticmethod
    def _get_bucket_name_and_key(path: str) -> Tuple[str, str]:
        parsed_path = urlparse(path)
        bucket_name = parsed_path.netloc
        key = parsed_path.path.lstrip("/")
        return bucket_name, key

    def _get_blob_size(self, blob: gcs.Blob) -> int:
        blob.reload()
        if blob.size is None:
            raise ValueError(f"Failed to get size for blob: {blob.name}")
        return blob.size

    def _is_file(self, bucket_name: str, key: str) -> bool:
        # print(bucket_name, key)
        bucket = self.gcs_client.bucket(bucket_name)
        blob = bucket.blob(key)
        try:
            blob.reload()
            print(blob.name)
            return True
        except NotFound:
            return False

    def _get_size(self, bucket_name: str, key: str) -> int:
        bucket = self.gcs_client.bucket(bucket_name)
        blob = bucket.get_blob(key)
        if blob is None:
            raise ValueError(f"Getting size for invalid object with bucket | key: {bucket_name} | {key}")

        return self._get_blob_size(blob)

    def _download_file(self, bucket_name: str, key: str) -> str:
        extension = "".join(Path(key).suffixes)
        temp_file = self.local_fs_adapter.create_temp_file(suffix=extension)

        bucket = self.gcs_client.bucket(bucket_name)
        blob = bucket.get_blob(key)
        if blob is None:
            raise ValueError(f"Downloading invalid object with bucket | key: {bucket_name} | {key}")
        blob.download_to_filename(temp_file)
        return temp_file

    def _get_directory_entries(self, bucket_name: str, key: str, no_files: bool = False, max_file_size: Optional[float] = None) -> List[str]:
        bucket = self.gcs_client.bucket(bucket_name)
        # Setting max_results to 10,000 as a reasonable caution that a directory should not have
        # more than 10,000 entries.
        # Using delimiter causes result to have directory-like structure
        blobs = bucket.list_blobs(max_results=10_000, prefix=key, delimiter="/")

        entries: List[str] = []
        for blob in blobs:
            blob: gcs.Blob

            if no_files:
                # Note: We need to iterate through (or otherwise act on?) the blobs to populate blob.prefixes
                # Thus we no-op here rather than skipping the loop
                continue

            size: int = self._get_blob_size(blob)
            if max_file_size is not None and size > max_file_size:
                log.info(
                    "Blob %s has size %.2fGb exceeding max file size %.2fGb, skipping.",
                    blob.name,
                    size / 1e9,
                    max_file_size / 1e9,
                )
                continue

            entries.append(blob.name)  # type: ignore

        # Note: We need to iterate through (or otherwise act on?) the blobs to populate blob.prefixes
        entries += blobs.prefixes

        return [entry.removeprefix(key) for entry in entries]

    def _list_entries(self, path: str, no_files: bool = False, max_file_size: Optional[float] = None) -> List[str]:
        bucket_name, key = self._get_bucket_name_and_key(path)

        if self.local_fs_adapter.has_supported_archive_extension(path):
            file_path = self._download_file(bucket_name, key)

            if no_files:
                return self.local_fs_adapter.list_dirs(file_path)
            return self.local_fs_adapter.list_entries(file_path, max_file_size)

        if self._is_file(bucket_name, key):
            # print(bucket_name, key)
            raise ValueError(f"Path corresponds to a file without a supported archive extension {path}")

        res = self._get_directory_entries(bucket_name, key, no_files=no_files, max_file_size=max_file_size)
        # print('Result', res)
        return res

    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        return self._list_entries(path, max_file_size=max_file_size)

    def list_dirs(self, path: str) -> List[str]:
        return self._list_entries(path, no_files=True)

    def delete_path(self, path: str):
        bucket_name, key = self._get_bucket_name_and_key(path)

        bucket = self.gcs_client.bucket(bucket_name)
        # Not using delimiter causes result to not have directory-like structure (all blobs returned)
        blobs = list(bucket.list_blobs(prefix=key))

        # blob_names = []
        # for blob in blobs:
        #     blob_names.append(blob.name)
        bucket.delete_blobs(blobs)

        # print(len(blob_names))
        raise NotImplementedError()

    def is_file(self, path: str) -> bool:
        bucket_name, key = self._get_bucket_name_and_key(path)

        return self._is_file(bucket_name, key)

    def is_dir(self, path: str) -> bool:
        bucket_name, key = self._get_bucket_name_and_key(path)
        bucket = self.gcs_client.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix=key, max_results=1))

        return not self._is_file(bucket_name, key) and len(blobs) > 0

    def download_to_folder(self, path: str, local_dest_folder: str):
        bucket_name, key = self._get_bucket_name_and_key(path)
        bucket = self.gcs_client.bucket(bucket_name)

        blobs: List[gcs.Blob] = list(bucket.list_blobs(prefix=key))
        for blob in blobs:
            if not blob.name:
                raise NotImplementedError()
            blob_path: str = blob.name
            blob_local_dest = blob_path.replace(key, local_dest_folder)
            print(blob_local_dest)
            blob.download_to_filename(blob_local_dest)

    def upload(self, path: str, local_src: str):
        raise NotImplementedError()


class S3StorageAdapter(StorageAdapter):
    def __init__(self, endpoint_url: Optional[str] = None):
        super().__init__()
        self._s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            config=Config(retries={"max_attempts": 10, "mode": "standard"}),
            use_ssl=not int(os.environ.get("OLMO_NO_SSL", "0")),
        )

        # print(self._s3_client.head_bucket(Bucket="olmo-checkpoints"))

        self._local_fs_adapter: Optional[LocalFileSystemAdapter] = None
        self._temp_dirs: List[tempfile.TemporaryDirectory] = []

    @property
    def local_fs_adapter(self):
        if self._local_fs_adapter is None:
            self._local_fs_adapter = LocalFileSystemAdapter()

        return self._local_fs_adapter

    @staticmethod
    def _get_bucket_name_and_key(path: str) -> Tuple[str, str]:
        parsed_path = urlparse(path)
        bucket_name = parsed_path.netloc
        key = parsed_path.path.lstrip("/")
        return bucket_name, key

    def _get_directory_entries(self, bucket_name: str, key: str, no_files: bool = False, max_file_size: Optional[float] = None) -> List[str]:
        response: Dict[str, Any] = self._s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key, Delimiter="/")

        entries: List[str] = []

        if not no_files:
            objects_metadata: List[Dict[str, Any]] = response.get('Contents', [])
            for object_metadata in objects_metadata:
                object_name = object_metadata['Key']

                size: int = object_metadata['Size']
                if max_file_size is not None and size > max_file_size:
                    log.info(
                        "Object %s has size %.2fGb exceeding max file size %.2fGb, skipping.",
                        object_name,
                        size / 1e9,
                        max_file_size / 1e9,
                    )
                    continue

                entries.append(object_name)

        directories_metadata: List[Dict[str, str]] = response.get('CommonPrefixes', [])
        entries += [
            directory_metadata['Prefix']
            for directory_metadata in directories_metadata
        ]

        return [entry.removeprefix(key) for entry in entries]

    def _list_entries(self, path: str, no_files: bool = False, max_file_size: Optional[float] = None) -> List[str]:
        bucket_name, key = self._get_bucket_name_and_key(path)

        if self.local_fs_adapter.has_supported_archive_extension(path):
            raise NotImplementedError()
            # file_path = self._download_file(bucket_name, key)

            # if no_files:
            #     return self.local_fs_adapter.list_dirs(file_path)
            # return self.local_fs_adapter.list_entries(file_path, max_file_size)

        if self._is_file(bucket_name, key):
            raise ValueError(f"Path corresponds to a file without a supported archive extension {path}")

        res = self._get_directory_entries(bucket_name, key, no_files=no_files, max_file_size=max_file_size)
        return res

    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        return self._list_entries(path, max_file_size=max_file_size)

    def list_dirs(self, path: str) -> List[str]:
        return self._list_entries(path, no_files=True)

    def delete_path(self, path: str):
        bucket_name, key = self._get_bucket_name_and_key(path)

        response: Dict[str, Any] = self._s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key)

        objects_metadata: List[Dict[str, Any]] = response.get('Contents', [])
        object_keys_to_delete: List[str] = [
            object_metadata['Key']
            for object_metadata in objects_metadata
        ]

        log.info("Starting to delete %d objects at %s", len(object_keys_to_delete), path)

        max_delete_batch_size: int = 1000
        for i in range(len(object_keys_to_delete), max_delete_batch_size):

            delete_batch_keys = {
                'Key': object_key
                for object_key in object_keys_to_delete[i:i + max_delete_batch_size]
            }

            delete_response: Dict[str, Any] = self._s3_client.delete_objects(
                Bucket=bucket_name,
                Delete=delete_batch_keys)

            errors: List[Dict[str, Any]] = delete_response.get('Errors', [])
            if len(errors) > 0:
                for error in errors:
                    log.error("Failed to delete %s with code %s, message %s", error['Key'], error['Code'], error['Message'])

                raise RuntimeError(f"Error occurred during deletion at {path}")

            deleted_object_keys = [
                deleted_object['Key']
                for deleted_object in delete_response.get('Deleted', [])
            ]
            delete_batch_keys_set = set(delete_batch_keys)
            deleted_object_keys_set = set(deleted_object_keys)
            unrequested_deleted_keys = deleted_object_keys_set.difference(delete_batch_keys_set)
            if len(unrequested_deleted_keys) > 0:
                raise RuntimeError(f"The following keys were unexpectedly deleted: {unrequested_deleted_keys}")
            undeleted_keys = delete_batch_keys_set.difference(deleted_object_keys_set)
            if len(undeleted_keys) > 0:
                raise RuntimeError(f"The following keys failed to be deleted: {undeleted_keys}")

    def _is_file(self, bucket_name: str, key: str) -> bool:
        try:
            self._s3_client.head_object(Bucket=bucket_name, Key=key)
            return True
        except boto_exceptions.ClientError as e:
            if int(e.response["Error"]["Code"]) == 404:
                return False

            raise e

    def is_file(self, path: str) -> bool:
        bucket_name, key = self._get_bucket_name_and_key(path)

        return self._is_file(bucket_name, key)

    def _is_dir(self, bucket_name: str, key: str) -> bool:
        if self._is_file(bucket_name, key):
            return False

        response = self._s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key, MaxKeys=1)
        return 'Contents' in response

    def is_dir(self, path: str) -> bool:
        bucket_name, key = self._get_bucket_name_and_key(path)

        return self._is_dir(bucket_name, key)

    def download_to_folder(self, path: str, local_dest_folder: str):
        bucket_name, key = self._get_bucket_name_and_key(path)

        response = self._s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key)
        objects_metadata: List[Dict[str, Any]] = response['Contents']
        for object_metadata in objects_metadata:
            object_key = object_metadata['Key']
            object_local_dest = object_key.replace(key, local_dest_folder)
            print(object_local_dest)

            self._s3_client.download_file(bucket_name, key, object_local_dest)

    def upload(self, path: str, local_src: str):
        if self.local_fs_adapter.is_file(local_src):
            bucket_name, key = self._get_bucket_name_and_key(path)
            self._s3_client.upload_file(local_src, bucket_name, key)

        elif self.local_fs_adapter.is_dir(local_src):
            for dirpath, _, filenames in os.walk(local_src):
                for filename in filenames:
                    local_filepath = os.path.join(dirpath, filename)
                    dest_filepath = local_filepath.replace(local_src, path)
                    bucket_name, key = self._get_bucket_name_and_key(dest_filepath)

                    self._s3_client.upload_file(local_filepath, bucket_name, key)

        else:
            raise ValueError(f"Local source {local_src} does not correspond to a valid file or directory")


class StorageCleaner:
    def __init__(
        self,
        dry_run: bool = False,
        ignore_prompts: bool = False,
        runs_require_config_yaml: bool = True,
        r2_account_id: Optional[str] = None,
        max_archive_size: Optional[float] = None,
        default_wandb_entity: Optional[str] = None,
        default_wandb_project: Optional[str] = None,
    ) -> None:
        self._dry_run: bool = dry_run
        self._runs_require_config_yaml = runs_require_config_yaml
        self._ignore_prompts: bool = ignore_prompts
        self._r2_account_id: Optional[str] = r2_account_id
        self._max_archive_size: Optional[float] = max_archive_size
        self._default_wandb_entity: Optional[str] = default_wandb_entity
        self._default_wandb_project: Optional[str] = default_wandb_project
        self._storage_adapters: Dict[StorageType, StorageAdapter] = {}

    def _get_storage_adapter(self, storage_type: StorageType) -> StorageAdapter:
        if storage_type not in self._storage_adapters:
            self._storage_adapters[storage_type] = StorageAdapter.create_storage_adapter(storage_type, self._r2_account_id)

        return self._storage_adapters[storage_type]

    def _get_storage_adapter_for_path(self, path: str) -> StorageAdapter:
        storage_type = StorageAdapter.get_storage_type_for_path(path)
        return self._get_storage_adapter(storage_type)

    @staticmethod
    def _contains_checkpoint_dir(dir_entries: List[str]) -> bool:
        return any(re.match(r"step\d+(-unsharded)?", entry) is not None for entry in dir_entries)

    @staticmethod
    def _contains_nontrivial_checkpoint_dir(dir_entries: List[str]) -> bool:
        return any(re.match(r"step[1-9]\d*(-unsharded)?", entry) is not None for entry in dir_entries)

    def _verify_deletion_without_checkpoint_dir(self, run_dir_entry: str):
        msg = f"No checkpoint dir found in run directory entry {run_dir_entry}. This entry might not correspond to a run."
        if self._runs_require_config_yaml:
            raise ValueError(msg)

        log.warning(msg)

        if not self._ignore_prompts:
            while True:
                response = input(f"{msg} Would you still like to delete {run_dir_entry}? (y/n) ")
                if response.lower() == "y":
                    break
                else:
                    raise ValueError(msg)

    def _delete_if_bad_run(self, storage: StorageAdapter, run_dir_entry: str):
        dir_entries = storage.list_entries(run_dir_entry)

        if not self._contains_checkpoint_dir(dir_entries):
            self._verify_deletion_without_checkpoint_dir(run_dir_entry)

        if not self._contains_nontrivial_checkpoint_dir(dir_entries):
            if self._dry_run:
                log.info("Would delete run_dir_entry %s", run_dir_entry)
            else:
                log.info("Deleting run_dir_entry %s", run_dir_entry)
                storage.delete_path(run_dir_entry)

    def delete_bad_runs(self, runs_path: str):
        log.info("Starting deletion of bad runs")

        if not runs_path.endswith("/"):
            raise ValueError(
                "Runs path does not end with '/'. Please verify that path is a directory and re-run with trailing '/'."
            )

        storage: StorageAdapter = self._get_storage_adapter_for_path(runs_path)
        run_dirs_entries = [
            os.path.join(runs_path, entry)
            for entry in storage.list_entries(runs_path, max_file_size=self._max_archive_size)
        ]
        for run_dir_entry in run_dirs_entries:
            self._delete_if_bad_run(storage, run_dir_entry)

    def _get_wandb_id(self, storage: StorageAdapter, run_dir_entry: str) -> str:
        dir_entries = storage.list_entries(run_dir_entry)
        if CONFIG_YAML not in dir_entries:
            raise FileNotFoundError(f'{CONFIG_YAML} not found in dir {run_dir_entry}, cannot get wandb id')

        config_yaml_path = os.path.join(run_dir_entry, CONFIG_YAML)
        train_config = TrainConfig.load(config_yaml_path)
        if train_config.wandb is None:
            raise ValueError(f'No wandb settings in config file {config_yaml_path}')

        entity_name = train_config.wandb.entity or self._default_wandb_entity
        project_name = train_config.wandb.project or self._default_wandb_project
        run_name = train_config.wandb.name

        if entity_name is None:
            raise ValueError(f'No wandb entity set in cli or in config file {config_yaml_path}')
        if project_name is None:
            raise ValueError(f'No wandb project name set in cli or in config file {config_yaml_path}')
        if run_name is None:
            raise ValueError(f'No wandb name set in config file {config_yaml_path}')

        wandb_api = wandb.Api()
        runs = list(wandb_api.runs(path=f'{entity_name}/{project_name}', filters={"display_name": {"$regex": run_name}}))
        if len(runs) == 0:
            raise ValueError(f'No wandb runs found for {run_dir_entry}')
        if len(runs) > 1:
            raise ValueError(f'{len(runs)} runs found for {run_dir_entry}')

        run = runs[0]
        print('id', run.id)
        return run.id

    def rename_runs_to_wandb_ids(self, runs_path: str):
        log.info("Starting renaming runs to their wandb ids")

        if not runs_path.endswith("/"):
            raise ValueError(
                "Runs path does not end with '/'. Please verify that path is a directory and re-run with trailing '/'."
            )

        storage: StorageAdapter = self._get_storage_adapter_for_path(runs_path)
        run_dir_entries = [
            os.path.join(runs_path, entry)
            for entry in storage.list_dirs(runs_path)
        ]

        print(run_dir_entries)
        run_wandb_ids = {
            run_dir_entry: self._get_wandb_id(storage, run_dir_entry)
            for run_dir_entry in run_dir_entries
        }
        print(run_wandb_ids)

        raise NotImplementedError

    def _is_sharded_checkpoint_dir(self, storage: StorageAdapter, directory: str) -> bool:
        return storage.is_dir(directory) and re.search(r"/step\d+/?$", directory) is not None

    @staticmethod
    def _get_checkpoint_number(checkpoint_dir: str) -> int:
        checkpoint_dir = checkpoint_dir.rstrip("/")
        checkpoint_dir = checkpoint_dir.removesuffix("-unsharded")
        match = re.search(r"/step(\d+)$", checkpoint_dir)
        if match is None:
            raise ValueError(f"Failed to find checkpoint number for dir {checkpoint_dir}")

        return int(match.group(1))

    def _get_sharded_checkpoint_dirs(self, storage: StorageAdapter, run_path: str, latest_checkpoint_only: bool) -> List[str]:
        if storage.is_file(run_path):
            local_storage = self._get_storage_adapter(StorageType.LOCAL_FS)
            assert isinstance(local_storage, LocalFileSystemAdapter)
            if not local_storage.has_supported_archive_extension(run_path):
                log.info('Trying to get sharded checkpoints from non-archive file %s, skipping', run_path)
                return []

            temp_dir = local_storage.create_temp_dir()
            storage.download_to_folder(run_path, temp_dir)

            storage = local_storage
            run_path = temp_dir

        run_subdirectories = [
            os.path.join(run_path, entry)
            for entry in storage.list_dirs(run_path)
        ]
        sharded_checkpoint_directories = list(filter(lambda subdirectory: self._is_sharded_checkpoint_dir(storage, subdirectory), run_subdirectories))

        if latest_checkpoint_only:
            latest_checkpoint_directory = max(sharded_checkpoint_directories, default=None, key=self._get_checkpoint_number)
            sharded_checkpoint_directories = [latest_checkpoint_directory] if latest_checkpoint_directory is not None else []

        # print('Test', run_subdirectories, sharded_checkpoint_directories)

        return sharded_checkpoint_directories

    def _unshard_checkpoint(self, sharded_checkpoint_dir: str, dest_dir: str):
        local_storage = self._get_storage_adapter(StorageType.LOCAL_FS)
        assert isinstance(local_storage, LocalFileSystemAdapter)
        local_sharded_temp_dir = local_storage.create_temp_dir()
        local_unsharded_temp_dir = local_storage.create_temp_dir()

        src_storage = self._get_storage_adapter_for_path(sharded_checkpoint_dir)
        src_storage.download_to_folder(sharded_checkpoint_dir, local_sharded_temp_dir)

        subprocess.run(["python", "scripts/unshard.py", local_sharded_temp_dir, local_unsharded_temp_dir], check=True)

        dest_storage = self._get_storage_adapter_for_path(dest_dir)
        dest_storage.upload(dest_dir, local_unsharded_temp_dir)

    def _unshard_checkpoints(self, runs_storage: StorageAdapter, run_path: str, checkpoints_dest_dir: str, latest_checkpoint_only: bool):
        sharded_checkpoint_directories = self._get_sharded_checkpoint_dirs(runs_storage, run_path, latest_checkpoint_only)
        for sharded_checkpoint_directory in sharded_checkpoint_directories:
            _, directory_name = os.path.split(sharded_checkpoint_directory.rstrip("/"))

            unsharded_checkpoint_directory_in_source = f"{os.path.join(run_path, directory_name)}-unsharded"
            if runs_storage.is_dir(unsharded_checkpoint_directory_in_source):
                log.info("Unsharded directory already exists for %s at source %s, skipping", sharded_checkpoint_directory, unsharded_checkpoint_directory_in_source)
                continue

            unsharded_checkpoint_dest_directory = f"{os.path.join(checkpoints_dest_dir, directory_name)}-unsharded"
            dest_storage = self._get_storage_adapter_for_path(unsharded_checkpoint_dest_directory)
            if dest_storage.is_dir(unsharded_checkpoint_directory_in_source):
                log.info("Unsharded directory already exists for %s at destination %s, skipping", sharded_checkpoint_directory, unsharded_checkpoint_dest_directory)
                continue

            if self._dry_run:
                log.info("Would unshard sharded checkpoint %s to %s", sharded_checkpoint_directory, unsharded_checkpoint_dest_directory)
            else:
                log.info("Unsharding sharded checkpoint %s to %s", sharded_checkpoint_directory, unsharded_checkpoint_dest_directory)
                self._unshard_checkpoint(sharded_checkpoint_directory, unsharded_checkpoint_dest_directory)

    def unshard_runs_checkpoints(self, runs_source_path: str, runs_dest_path: str, latest_checkpoint_only: bool):
        log.info("Starting unsharding checkpoints")

        if not runs_source_path.endswith("/"):
            raise ValueError(
                "Runs path does not end with '/'. Please verify that path is a directory and re-run with trailing '/'."
            )
        if not runs_dest_path.endswith("/"):
            raise ValueError(
                "Checkpoints destination directory does not end with '/'. Please verify that path is a directory and re-run with trailing '/'."
            )

        storage: StorageAdapter = self._get_storage_adapter_for_path(runs_source_path)
        runs_dir_entries = [
            os.path.join(runs_source_path, entry)
            for entry in storage.list_entries(runs_source_path, max_file_size=self._max_archive_size)
        ]

        for run_dir_entry in runs_dir_entries:
            self._unshard_checkpoints(storage, run_dir_entry, run_dir_entry.replace(runs_source_path, runs_dest_path), latest_checkpoint_only)


def perform_operation(args: argparse.Namespace):
    if args.dry_run:
        log.info("Dry run, no actions will be taken")

    if args.op == CleaningOperations.DELETE_BAD_RUNS:
        storage_cleaner = StorageCleaner(
            dry_run=args.dry_run,
            ignore_prompts=args.yes,
            runs_require_config_yaml=args.runs_require_config_yaml,
            r2_account_id = args.r2_account_id,
            max_archive_size=args.max_archive_size,
        )
        storage_cleaner.delete_bad_runs(args.runs_path)
    if args.op == CleaningOperations.RENAME_RUNS_TO_WANDB_ID:
        storage_cleaner = StorageCleaner(
            dry_run=args.dry_run,
            ignore_prompts=args.yes,
            r2_account_id = args.r2_account_id,
            default_wandb_entity=args.entity,
            default_wandb_project=args.project,
        )
        storage_cleaner.rename_runs_to_wandb_ids(args.runs_path)
    if args.op == CleaningOperations.UNSHARD_CHECKPOINTS:
        storage_cleaner = StorageCleaner(
            dry_run=args.dry_run,
            ignore_prompts=args.yes,
            r2_account_id = args.r2_account_id,
            max_archive_size=args.max_archive_size,
        )
        storage_cleaner.unshard_runs_checkpoints(args.runs_src_path, args.runs_dest_path, args.latest_checkpoint_only)


def _add_delete_subparser(subparsers: _SubParsersAction):
    delete_runs_parser = subparsers.add_parser(
        "clean", help="Delete bad runs (example no non-trivial checkpoints)"
    )
    delete_runs_parser.set_defaults(op=CleaningOperations.DELETE_BAD_RUNS)
    delete_runs_parser.add_argument(
        "runs_path",
        help="Path to directory containing one or more run directories",
    )
    delete_runs_parser.add_argument(
        "--require_config_yaml",
        action="store_true",
        dest="runs_require_config_yaml",
        help=f"Enforces without prompt the sanity check that an entry being deleted has a {CONFIG_YAML} file (and so is a run)",
    )
    delete_runs_parser.add_argument(
        "--max_archive_size",
        default=DEFAULT_MAX_ARCHIVE_SIZE,
        help="Max size archive files to consider for deletion (in bytes). Any archive larger than this is ignored/not deleted.",
    )


def _add_wandb_subparser(subparsers: _SubParsersAction):
    wandb_runs_parser = subparsers.add_parser(
        "rename_to_wandb", help="renames runs to their wandb ids"
    )
    wandb_runs_parser.set_defaults(op=CleaningOperations.RENAME_RUNS_TO_WANDB_ID)
    wandb_runs_parser.add_argument(
        "runs_path",
        help="Path to directory containing one or more run directories",
    )
    wandb_runs_parser.add_argument(
        "--entity",
        default=WANDB_ENTITY,
        help="Wandb entity to use for runs without a specified entity.",
    )
    wandb_runs_parser.add_argument(
        "--project",
        default=None,
        help="Wandb project to use for runs without a specified project. If unset, runs without a specified project will be skipped.",
    )


def _add_unsharding_subparser(subparsers: _SubParsersAction):
    unsharding_runs_parser = subparsers.add_parser(
        "unshard", help="unshard checkpoint(s) of each run"
    )
    unsharding_runs_parser.set_defaults(op=CleaningOperations.UNSHARD_CHECKPOINTS)
    unsharding_runs_parser.add_argument(
        "runs_src_path",
        help="Path to directory containing one or more run directories",
    )
    unsharding_runs_parser.add_argument(
        "runs_dest_path",
        help="Path to directory where runs with unsharded checkpoints should be output (only the unsharded checkpoints are stored)",
    )
    unsharding_runs_parser.add_argument(
        "--latest_checkpoint_only",
        action="store_true",
        help="If set, only the latest checkpoint of each run (if sharded) is unsharded.",
    )
    unsharding_runs_parser.add_argument(
        "--max_archive_size",
        default=DEFAULT_MAX_ARCHIVE_SIZE,
        help="Max size archive run files to consider for unsharding (in bytes). Any archive larger than this is skipped.",
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument(
        "-n",
        "--dry_run",
        action="store_true",
        help="If set, indicate actions but do not do them",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="If set, bypass prompts",
    )
    parser.add_argument(
        "-l",
        "--log_level",
        default="INFO",
        help="Sets the logging level",
    )
    parser.add_argument(
        "--r2_account_id",
        default=R2_ACCOUNT_ID,
        help="Account id for R2 cloud storage",
    )

    subparsers = parser.add_subparsers(dest="command", help="Cleaning commands", required=True)
    _add_delete_subparser(subparsers)
    _add_wandb_subparser(subparsers)
    _add_unsharding_subparser(subparsers)

    # gs://ai2-olmo/ai2-llm/olmo-medium/njmmt4v8/config.yaml
    # temp
    # gs://ai2-olmo/unsorted-checkpoints/3416090.tar.bz2
    # r2://olmo-checkpoints/ai2-llm/olmo-medium/ips8ixw7/
    # s3://ai2-llm/checkpoints/1b/

    return parser


def main():
    args = get_parser().parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    perform_operation(args)


if __name__ == "__main__":
    main()
