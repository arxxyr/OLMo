perplexity_dir=$1

# get script directory
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

python $SCRIPT_DIR/subsetter.py \
    --input_files $perplexity_dir/raw/twitterAAE_HELM_fixed/*.gz \
    --output_dir $perplexity_dir/v3/twitterAAE_HELM_fixed \
    --seed 42 \
    --sample_evenly_by_file

mkdir -p $perplexity_dir/v3/m2d2/s2orc
python $SCRIPT_DIR/m2d2_subsetter.py \
    --input_files $perplexity_dir/raw/m2d2/s2orc/*/valid.txt \
    --output_dir $perplexity_dir/v3/m2d2_s2orc_unsplit/val \
    --tokenizer "EleutherAI/gpt-neox-20b" \
    --tokens_per_subdomain 100000

python $SCRIPT_DIR/m2d2_subsetter.py \
    --input_files $perplexity_dir/raw/m2d2/s2orc/*/test.txt \
    --output_dir $perplexity_dir/v3/m2d2_s2orc_unsplit/test \
    --tokenizer "EleutherAI/gpt-neox-20b" \
    --tokens_per_subdomain 100000

python $SCRIPT_DIR/subsetter.py \
    --input_files $perplexity_dir/v0/4chan_meta_sep/*.jsonl.gz \
    --output_dir $perplexity_dir/v3/4chan_meta_sep \
    --seed 42 \
    --tokenizer "EleutherAI/gpt-neox-20b" \
    --split_token_count_target 1000000

python $SCRIPT_DIR/subsetter.py \
    --input_files $perplexity_dir/v0/manosphere_meta_sep/*.jsonl.gz \
    --output_dir $perplexity_dir/v3/manosphere_meta_sep \
    --seed 42 \
    --tokenizer "EleutherAI/gpt-neox-20b" \
    --split_token_count_target 1000000

python $SCRIPT_DIR/subsetter.py \
    --input_files $perplexity_dir/v0/ice_fixed/*.gz \
    --output_dir $perplexity_dir/v3/ice_fixed \
    --seed 42 \
    --sample_evenly_by_file