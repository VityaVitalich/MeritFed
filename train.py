import os
import yaml
import argparse
import sys
print("Received arguments:", sys.argv)

SAVING_DIR = os.environ.get("SAVING_DIR")
HF_TOKEN = os.environ.get("HF_TOKEN")
os.environ["TRANSFORMERS_CACHE"] = SAVING_DIR + "hf_cache/"
os.environ["HF_HOME"] = SAVING_DIR + "hf_cache/"
os.environ["TOKENIZERS_PARALLELISM"] = "true"



import sys
import torch
import pandas as pd
from torch import nn
import numpy as np
from torch.optim.lr_scheduler import ExponentialLR
import wandb
from dataclasses import dataclass, field
from apex.optimizers import FusedAdam

from pipeline_src.train import train
from pipeline_src.logger.logger import WanDBWriter
from pipeline_src.trainer.train_epoch import train_epoch, predict
from pipeline_src.dataset.dataset import init_data
from pipeline_src.optimizers import MeritFedMD, MeritFedAdam, MeritFedParallelMD

print(torch.cuda.is_available())
if torch.cuda.is_available():
    device = "cuda"
    print(f"GPU with {torch.cuda.device_count()} devices")
else:
    device = "cpu"
    print("CPU")



from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    AutoConfig,
    AutoModelForCausalLM,
    M2M100ForConditionalGeneration,
    M2M100Tokenizer

)

from peft import (
    LoraConfig,
    get_peft_model,
    get_peft_model_state_dict,
    prepare_model_for_kbit_training,
)

from accelerate import Accelerator, DistributedDataParallelKwargs

def read_args(params_list):
    global config


    config.n_epochs = params_list["EPOCHS"][0]
    config.batch_size = params_list["BATCH_SIZE"][0]
    config.lr = float(params_list["LR"][0])
    config.min_lr = float(params_list["MIN_LR"][0])
    config.validation = int(params_list["VAL_EVERY_EPOCH"][0])
    config.save_every = int(params_list["SAVE_EVERY"][0])
    config.max_seq_len = int(params_list["MAX_SEQ_LEN"][0])
    config.max_steps = int(params_list["MAX_STEPS"][0])
    config.data_path = params_list["DATA_PATH"][0]
    config.device = device
    config.using_peft = params_list["USING_PEFT"][0]
    config.model_type = params_list["MODEL_TYPE"][0] 
    config.wandb_log_dir = SAVING_DIR + "wandb/"
    config.model_checkpoint = params_list["MODEL_CHECKPOINT"][0]
    config.exp_name = (params_list["RUN_NAME"][0]
    )
    config.save_strategy = params_list["SAVE_STRATEGY"][0]
    config.saving_path = SAVING_DIR + "model_checkpoints/" + config.exp_name + '_seed' + str(SEED)
    config.log_pred_every = params_list["LOG_PRED_EVERY"][0]
    config.target_lang = params_list["TARGET_LANG"][0]
    config.compute_metrics_every = params_list["COMPUTE_METRICS_EVERY"][0]
    config.saving_predictions_path = SAVING_DIR + "model_outputs/"
    config.fl = params_list["FL"][0]
    config.adaptive_batch_size = params_list["ADAPTIVE_BATCH_SIZE"][0]
    config.total_batch_size = params_list["TOTAL_BATCH_SIZE"][0]
    config.min_batch_size = params_list["MIN_BATCH_SIZE"][0]
    config.max_batch_size = params_list["MAX_BATCH_SIZE"][0]
    config.fl_lr = params_list["FL_LR"][0]
    config.fl_niters = params_list["FL_NITERS"][0]
    config.use_adam = params_list["AUX_ADAM"][0]
    config.fl_beta_1 = params_list["FL_BETA_1"][0]
    config.drop_threshold = params_list["DROP_THRESHOLD"][0]
    config.enable_fl_every = params_list["ENABLE_FL_EVERY"][0]

    config.gen_args =  {
    "no_repeat_ngram_size": params_list["NO_REPEAT_NGRAM"][0],
    "do_sample": params_list["SAMPLING"][0],
    "max_new_tokens": 256,
    "temperature": params_list["TEMPERATURE"][0],
    "top_k": params_list["TOP_K"][0],
    "num_return_sequences": 1,
    "num_beams": params_list["NUM_BEAMS"][0],   
    }

@dataclass
class TaskConfig:
    project_name: str = "MTFL"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Model")
    parser.add_argument('--main_config', type=str, default='./configs/train.yml', help='Seed for training')
    parser.add_argument('--config_file', type=str, default='./default_config.yaml', help='accelerate config')
    parser.add_argument('--main_process_port', type=int, default=0, help='accelerate config')
    args = parser.parse_args()

    main_config_path = args.main_config
    with open(main_config_path, 'r') as file:
        params_list = yaml.load(file, Loader=yaml.FullLoader)
    
    config = TaskConfig()
    SEED = params_list["SEED"][0]
    torch.manual_seed(SEED)
    torch.cuda.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    np.random.seed(SEED)
    config.seed = SEED

    read_args(params_list)

    ddp_kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(log_with="wandb", kwargs_handlers=[ddp_kwargs])
    config.device = accelerator.device

    model_params = {}
    if config.model_type == "AutoLM":
        model_type = AutoModelForCausalLM
        tokenizer_type = AutoTokenizer
    elif config.model_type == "M2M100":
        model_type = M2M100ForConditionalGeneration
        tokenizer_type = M2M100Tokenizer
        
    elif config.model_type == "Seq2Seq":
        model_type = AutoModelForSeq2SeqLM
        tokenizer_type = AutoTokenizer

    model = model_type.from_pretrained(
        config.model_checkpoint,
        use_auth_token=HF_TOKEN,
        torch_dtype=torch.bfloat16,
        **model_params
    )

    if config.model_type == 'AutoLM':
        padding_side = 'left'
    else:
        padding_side = 'right'

    tokenizer = tokenizer_type.from_pretrained(
        config.model_checkpoint,
        use_auth_token=HF_TOKEN,
        padding_side=padding_side,
    )
    if not tokenizer.pad_token_id:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    if 'flores' in config.model_checkpoint:
        tokenizer.lang_token_to_id = {t: i for t, i in zip(tokenizer.all_special_tokens, tokenizer.all_special_ids) if i > 5}
        tokenizer.lang_code_to_token = {s.strip("_"): s for s in tokenizer.lang_token_to_id}
        tokenizer.lang_code_to_id = {s.strip("_"): i for s, i in tokenizer.lang_token_to_id.items()}
        tokenizer.id_to_lang_token = {i: s for s, i in tokenizer.lang_token_to_id.items()}


    if any(name in config.data_path for name in ['sma', 'sms', 'smn', 'sme']) and (config.target_lang == "fi") and (config.model_type == 'M2M100'):

        embedding_size = model.get_input_embeddings().weight.shape[0]
        if ('/home' in config.model_checkpoint):
            print(embedding_size)
            embedding_size = embedding_size - 4
        new_langs = ['sma', 'sms', 'smn', 'sme']
        for lang, new_lang_id in zip(new_langs, range(embedding_size, embedding_size + len(new_langs))):
            tokenizer.lang_token_to_id[f'__{lang}__'] = new_lang_id
            tokenizer.lang_code_to_id[lang] = new_lang_id
            tokenizer.lang_code_to_token[lang] = f'__{lang}__'
            tokenizer.id_to_lang_token[new_lang_id] = f'__{lang}__'
            tokenizer.added_tokens_encoder[f'__{lang}__'] = new_lang_id
            tokenizer.additional_special_tokens.append(f'__{lang}__')
        
        config.target_lang = 'fi'
        model.resize_token_embeddings( embedding_size + len(new_langs))
    
    if ('indian' in config.data_path.lower()) and (config.model_type == 'M2M100'):

        embedding_size = model.get_input_embeddings().weight.shape[0]
        if ('/home' in config.model_checkpoint):
            print(embedding_size)
            embedding_size = embedding_size - 4
        new_langs = ['lus', 'mni', 'as', 'kha']
        for lang, new_lang_id in zip(new_langs, range(embedding_size, embedding_size + len(new_langs))):
            tokenizer.lang_token_to_id[f'__{lang}__'] = new_lang_id
            tokenizer.lang_code_to_id[lang] = new_lang_id
            tokenizer.lang_code_to_token[lang] = f'__{lang}__'
            tokenizer.id_to_lang_token[new_lang_id] = f'__{lang}__'
            tokenizer.added_tokens_encoder[f'__{lang}__'] = new_lang_id
            tokenizer.additional_special_tokens.append(f'__{lang}__')
        
        #config.target_lang = 'multi'
        print(f'added {new_langs}')
        model.resize_token_embeddings(embedding_size + len(new_langs))

    if config.model_type == 'M2M100':
        config.gen_args['forced_bos_token_id'] = tokenizer.get_lang_id(config.target_lang)

    train_dataset, val_dataset, test_dataset, train_loader, val_loader, test_loader, weight_name_map = init_data(tokenizer, config)
    # Setup max steps and scheduler steps
    if config.fl:
        assert config.max_steps > 0, "FL only works with max steps"
        steps_per_loader = min([len(i) for i in train_loader])
        scheduler_steps = config.max_steps
        config.n_epochs = (config.max_steps // steps_per_loader) + 1
        config.weight_name_map = weight_name_map

    else:
        if config.max_steps == -1:
            config.max_steps = float('inf')
            scheduler_steps = len(train_loader) * config.n_epochs
        else:
            config.n_epochs = (config.max_steps // len(train_loader)) + 1
            scheduler_steps = config.max_steps


    model = accelerator.prepare_model(model)

    if config.fl:
        prepared_train_loaders = {w_id: accelerator.prepare_data_loader(current_loader) for w_id, current_loader in enumerate(train_loader)}
    else:
        prepared_train_loaders = accelerator.prepare_data_loader(train_loader)

    val_loader = accelerator.prepare(val_loader)
    test_loader = [accelerator.prepare(lang_test_loader) for lang_test_loader in test_loader]


    logger = WanDBWriter(accelerator, config)

    if config.fl:
        config.npeers = len(train_loader)
        config.mdlr_ = config.fl_lr
        config.mdniters_ = config.fl_niters
        
        if config.drop_threshold > 0:
            assert config.drop_threshold < (1 / config.npeers), "Drop Weight must be less than Uniform"

        optimizer_class = MeritFedParallelMD

        optimizer = optimizer_class(
            model.parameters(), config, val_loader=val_loader, model=model, accelerator=accelerator
        )
    else:
        optimizer = FusedAdam(
            model.parameters(), lr=config.lr, betas=(0.9, 0.98), eps=1e-9
        )

    optimizer = accelerator.prepare_optimizer(optimizer)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=scheduler_steps, eta_min=config.min_lr
    )
    scheduler = accelerator.prepare_scheduler(scheduler)


    train(
        model,
        tokenizer,
        prepared_train_loaders,
        val_loader,
        test_loader,
        optimizer,
        scheduler,
        logger,
        accelerator,
        config,
    )
