# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Fine-tuning the library models for language modeling on a text file (GPT, GPT-2, BERT, RoBERTa).
GPT and GPT-2 are fine-tuned using a causal language modeling (CLM) loss while BERT and RoBERTa are fine-tuned
using a masked language modeling (MLM) loss.
"""

from __future__ import absolute_import, division, print_function

import argparse
import pickle
from lib2to3.pgen2 import token
import logging
import os
import random
import json
from pathlib import Path

import torch
import numpy as np
import pandas as pd

from torch.utils.data import DataLoader, Dataset, SequentialSampler, RandomSampler, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from utils.early_stopping import EarlyStopping

from model import Model
from transformers import (WEIGHTS_NAME, get_linear_schedule_with_warmup,
                          RobertaConfig, RobertaModel, RobertaTokenizer)

from torch.optim import AdamW
from sklearn.metrics import accuracy_score, recall_score, precision_score, f1_score, roc_auc_score, auc, average_precision_score
from tqdm import tqdm
from interpret import attention_interpretation
from my_utils import ModelParameterCounter,RunTimeCounter

no_deprecation_warning=True
logger = logging.getLogger(__name__)
early_stopping = EarlyStopping()

class InputFeatures(object):
    """A single training/test features for a example."""
    def __init__(self,
                 input_tokens,
                 input_ids,
                 contrast_tokens,
                 contrast_ids,
                 label,
                 index
    ):
        self.input_tokens = input_tokens
        self.input_ids = input_ids
        self.contrast_tokens = contrast_tokens
        self.contrast_ids = contrast_ids
        self.label = label
        self.index = index


def convert_examples_to_features(js,tokenizer,args):
    """convert examples to token ids"""
    # code = ' '.join(js['code'].split())
    code = js['code']
    code_tokens = tokenizer.tokenize(code)[:args.block_size-4]
    source_tokens = [tokenizer.cls_token,"<encoder_only>",tokenizer.sep_token] + code_tokens + [tokenizer.sep_token]
    source_ids = tokenizer.convert_tokens_to_ids(source_tokens)
    padding_length = args.block_size - len(source_ids)
    source_ids += [tokenizer.pad_token_id] * padding_length

    # contrast = ' '.join(js['contrast'].split())
    contrast = js['contrast']
    contrast_tokens = tokenizer.tokenize(contrast)[:args.block_size-4]
    contrast_tokens = [tokenizer.cls_token,"<encoder_only>",tokenizer.sep_token] + contrast_tokens + [tokenizer.sep_token]
    contrast_ids = tokenizer.convert_tokens_to_ids(contrast_tokens)
    padding_length = args.block_size - len(contrast_ids)
    contrast_ids += [tokenizer.pad_token_id] * padding_length

    return InputFeatures(source_tokens,source_ids,
                         contrast_tokens,contrast_ids,
                         js['label'],js['index'])


class TextDataset(Dataset):
    def __init__(self, tokenizer, args, file_path=None):
        self.examples = []
        data = json.load(open(file_path,mode='r'))
        for js in tqdm(data):
            self.examples.append(convert_examples_to_features(js,tokenizer,args))
        if 'train' in file_path:
            for idx, example in enumerate(self.examples[:3]):
                    logger.info("*** Example ***")
                    logger.info("idx: {}".format(idx))
                    logger.info("label: {}".format(example.label))
                    logger.info("input_tokens: {}".format([x.replace('\u0120','_') for x in example.input_tokens]))
                    logger.info("input_ids: {}".format(' '.join(map(str, example.input_ids))))

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, i):
        input_ids = self.examples[i].input_ids
        contrast_ids = self.examples[i].contrast_ids
        label = self.examples[i].label
        index = self.examples[i].index
        return (torch.tensor(input_ids),torch.tensor(contrast_ids),
                torch.tensor(label),torch.tensor(index))


def set_seed(seed=42):
    random.seed(seed)
    os.environ['PYHTONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True


def train(args, train_dataset, model, tokenizer):
    """ Train the model """
    train_sampler = SequentialSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler,
                                  batch_size=args.train_batch_size,
                                  num_workers=16, pin_memory=True)

    args.max_steps = args.num_train_epochs * len(train_dataloader)

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.max_steps*0.1,
                                                num_training_steps=args.max_steps)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.train_batch_size // args.n_gpu )
    logger.info("  Total train batch size = %d", args.train_batch_size)
    logger.info("  Total optimization steps = %d", args.max_steps)

    losses, best_f1 = [], 0

    model.zero_grad()
    for idx in range(args.num_train_epochs):
        for step, batch in enumerate(train_dataloader):
            inputs = batch[0].to(args.device)
            contrasts = batch[1].to(args.device)
            labels = batch[2].to(args.device)

            model.train()
            loss, _, = model(inputs, contrasts, labels)

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

            # print('loss: {:.2f}'.format(loss.item()), end='\r')
            losses.append(loss.item())

            if (step+1)% 100==0:
                logger.info("epoch {} step {} loss {}".format(idx,step+1,round(np.mean(losses[-100:]),4)))

            optimizer.step()
            optimizer.zero_grad()
            scheduler.step()

        results, eval_loss ,_ , _ = evaluate(args, model, tokenizer, args.eval_data_file)

        # for key, value in results.items():
        #     logger.info("  %s = %s", key, round(value,4))

        if results['f1'] > best_f1:
            best_f1 = results['f1']
            logger.info("  "+"*"*20)
            logger.info("  Best f1:%s",round(best_f1,4))
            logger.info("  "+"*"*20)

            checkpoint_prefix = 'checkpoint-best-f1'
            output_dir = os.path.join(args.output_dir, '{}'.format(checkpoint_prefix))
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            output_dir = os.path.join(output_dir, 'model.bin')
            model_to_save = model.model if hasattr(model, 'module') else model
            torch.save(model_to_save.state_dict(), output_dir)
            logger.info("Saving model checkpoint to %s", output_dir)

        early_stopping(eval_loss)
        if early_stopping.early_stop:
            print("Early stopping")
            break


def evaluate(args, model, tokenizer, data_file):
    """ Evaluate the model """
    eval_dataset = TextDataset(tokenizer, args, data_file)
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler,
                                batch_size=args.eval_batch_size, num_workers=4)

    # Eval!
    logger.info("***** Running evaluation *****")
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)

    eval_loss = 0.0
    nb_eval_steps = 0
    model.eval()
    logits=[]
    labels=[]
    indices=[]
    for batch in eval_dataloader:
        input = batch[0].to(args.device)
        contrast = batch[1].to(args.device)
        label = batch[2].to(args.device)
        index = batch[3].to(args.device)
        with torch.no_grad():
            lm_loss, logit = model(input, contrast, label)
            eval_loss += lm_loss.mean().item()
            logits.append(logit.cpu().numpy())
            labels.append(label.cpu().numpy())
            indices.append(index.cpu().numpy())
        nb_eval_steps += 1
    logits = np.concatenate(logits,0)
    labels = np.concatenate(labels,0)
    indices = np.concatenate(indices,0)
    preds = logits[:, 1] > 0.5

    acc = accuracy_score(labels, preds)
    recall = recall_score(labels, preds)
    precision = precision_score(labels, preds)
    f1 = f1_score(labels, preds)
    pr_auc = average_precision_score(labels, logits[:, 1])
    roc_auc = roc_auc_score(labels, logits[:, 1])
    results = {
        "acc": float(acc),
        "recall": float(recall),
        "precision": float(precision),
        "f1": float(f1),
        "pr_auc": float(pr_auc),
        "roc_auc": float(roc_auc)
    }
    return results, eval_loss , indices , preds


def interpret(args, model, tokenizer, data_file):
    """ Interpret the model """
    eval_dataset = TextDataset(tokenizer, args, data_file)
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler,
                                batch_size=args.eval_batch_size, num_workers=4)

    # Eval!
    logger.info("***** Running evaluation *****")
    interpret_results = []
    for batch in tqdm(eval_dataloader):
        input = batch[0].to(args.device)
        contrast = batch[1].to(args.device)
        label = batch[2].to(args.device)
        dataset_id = batch[3].to(args.device)
        with torch.no_grad():
            bs_size = input.shape[0]
            lm_loss, logit , attentions = model(input, contrast, label,need_attentions=True)
            attentions = attentions[0]  # get first layer
            preds = logit.cpu().numpy()[:, 1] > 0.5


            for idx in range(bs_size):
                line_scores , line_token_level_scores = attention_interpretation(input[idx].cpu().tolist(),attentions[idx],tokenizer)
                interpret_results.append({
                    "id" : dataset_id[idx].item(),
                    "pred": preds[idx].item(),
                    "line_scores" : line_scores,
                    "line_token_level_scores" : line_token_level_scores
                })
    logger.info("interpret done!!")
    with open(Path(__file__).parent / "storage"  / "test_interpret.json" , mode='w') as f:
        json.dump(interpret_results , f)


def tSNE_embedding(args, model, tokenizer, data_file):
    """ get tSNE embeddings  """
    eval_dataset = TextDataset(tokenizer, args, data_file)
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler,
                                batch_size=args.eval_batch_size, num_workers=4)

    # Eval!
    logger.info("***** Running tSNE embeddings *****")
    all_tSNE_embedding = []
    for batch in tqdm(eval_dataloader):
        input = batch[0].to(args.device)
        contrast = batch[1].to(args.device)
        labels = batch[2].to(args.device)
        dataset_id = batch[3].to(args.device)
        with torch.no_grad():
            bs_size = input.shape[0]
            _, _ , tSNE_embedding = model(input, contrast, labels,output_tSNE_embedding=True)
            all_tSNE_embedding.extend(
                [(embedding, label) for embedding, label in zip(tSNE_embedding.tolist(), labels.tolist())])

    logger.info("tSNE done!!")
    with open(Path(__file__).parent / "storage"  / "test_tSNE_embedding.pkl" , mode='wb') as f:
        pickle.dump(all_tSNE_embedding , f)


def detect(args, model, tokenizer, data_file):
    detect_dataset = TextDataset(tokenizer, args, data_file)
    detect_sampler = SequentialSampler(detect_dataset)
    detect_dataloader = DataLoader(detect_dataset, sampler=detect_sampler,
                                batch_size=args.eval_batch_size, num_workers=4)

    eval_output_dir = args.output_dir
    if not os.path.exists(eval_output_dir):
        os.makedirs(eval_output_dir)

    # Detect!
    logger.info("***** Running detect *****")
    logger.info("  Num examples = %d", len(detect_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)

    nb_eval_steps = 0
    model.eval()
    logits=[]
    labels=[]
    indices=[]
    for batch in detect_dataloader:
        contrast = batch[1].to(args.device)
        label = batch[2].to(args.device)
        index = batch[3].to(args.device)
        with torch.no_grad():
            _, logit = model(contrast, None, label)
            logits.append(logit.cpu().numpy())
            labels.append(label.cpu().numpy())
            indices.append(index.cpu().numpy())
        nb_eval_steps += 1
    logits = np.concatenate(logits,0)
    labels = np.concatenate(labels,0)
    preds = logits[:, 1] > 0.5

    acc = accuracy_score(labels, preds)
    results = {
        "acc": float(acc)
    }
    return results, indices, preds

def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    # Other parameters
    parser.add_argument("--train_data_file", default=None, type=str,
                        help="The input training data file (a jsonl file).")
    parser.add_argument("--eval_data_file", default=None, type=str,
                        help="An optional input evaluation data file to evaluate the perplexity on (a jsonl file).")
    parser.add_argument("--test_data_file", default=None, type=str,
                        help="An optional input test data file to evaluate the perplexity on (a jsonl file).")
    parser.add_argument("--model_name_or_path", default=None, type=str,
                        help="The model checkpoint for weights initialization.")

    parser.add_argument("--block_size", default=-1, type=int,
                        help="Optional input sequence length after tokenization.")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_test", action='store_true',
                        help="Whether to run test on the dev set.")
    parser.add_argument("--do_detect", action='store_true',
                        help="Whether to run detect on the dev set.")
    parser.add_argument("--train_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--eval_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=1, type=int,
                        help="Total number of training epochs to perform.")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument('--simcse', action='store_true',
                        help="")
    parser.add_argument('--simct', action='store_true',
                        help="")
    parser.add_argument('--r_drop', action='store_true',
                        help="")
    parser.add_argument('--sigma', type=float, default=0.2,
                        help="")
    # Print arguments
    args = parser.parse_args()

    # Set log
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',level=logging.INFO )

    # Set device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args.n_gpu = torch.cuda.device_count()
    args.device = device
    logger.info("device: %s, n_gpu: %s", device, args.n_gpu)

    # Set seed
    set_seed(args.seed)

    # Build model
    tokenizer = RobertaTokenizer.from_pretrained(args.model_name_or_path)
    config = RobertaConfig.from_pretrained(args.model_name_or_path)
    model = RobertaModel.from_pretrained(args.model_name_or_path)

    model = Model(model, config, tokenizer, args)
    ModelParameterCounter().summary(model,'SVulD')
    logger.info("Training/evaluation parameters %s", args)

    model.to(args.device)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    time_counter = RunTimeCounter()
    # Training
    if args.do_train:
        train_dataset = TextDataset(tokenizer, args, args.train_data_file)
        train(args, train_dataset, model, tokenizer)
        time_counter.stop('SVulD train done!')


    # Testing
    if args.do_test:
        checkpoint_prefix = 'checkpoint-best-f1/model.bin'
        output_dir = os.path.join(args.output_dir, '{}'.format(checkpoint_prefix))
        model_to_load = model.module if hasattr(model, 'module') else model
        model_to_load.load_state_dict(torch.load(output_dir))
        # tSNE_embedding(args, model, tokenizer, args.test_data_file)
        # interpret(args, model, tokenizer, args.test_data_file)
        result, _ , indices, preds  = evaluate(args, model, tokenizer, args.test_data_file)
        logger.info("***** Test results *****")
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(round(result[key]*100 if "map" in key else result[key],4)))
        # print(indices)
        # print(preds)
        print(f"indices:{len(indices)} preds:{len(preds)}")
        dataframe = pd.DataFrame({"id": indices, "pred": preds})
        json.dump(dataframe.to_dict("records"), open(Path(__file__).parent / "storage" / "test.json" ,mode='w'))
        time_counter.stop('SVulD test done!')

    # Detect
    if args.do_detect:
        checkpoint_prefix = 'checkpoint-best-f1/model.bin'
        output_dir = os.path.join(args.output_dir, '{}'.format(checkpoint_prefix))
        model_to_load = model.module if hasattr(model, 'module') else model
        model_to_load.load_state_dict(torch.load(output_dir))
        result, indices, preds = detect(args, model, tokenizer, args.test_data_file)
        logger.info("***** Detect results *****")
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(round(result[key]*100 if "map" in key else result[key],4)))
        dataframe = pd.DataFrame({"index": indices, "pred": preds})
        dataframe.to_csv(f'{args.output_dir.replace("saved_models", "detect")}.csv', sep=',', index=False)


if __name__ == "__main__":
    main()