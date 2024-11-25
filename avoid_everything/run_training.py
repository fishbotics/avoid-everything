# MIT Licensempinete
#
# Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES, University of Washington. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import argparse
import logging
import os
import random
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

import lightning.pytorch as pl
import numpy as np
import torch
import yaml
from lightning.pytorch.callbacks import ModelCheckpoint, StochasticWeightAveraging

from avoid_everything.data_loader import DataModule
from avoid_everything.pretraining import PretrainingMotionPolicyTransformer
from avoid_everything.rope import ROPEMotionPolicyTransformer

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)

# Make deterministic
seed_value = 42
torch.manual_seed(seed_value)
random.seed(seed_value)
np.random.seed(seed_value)
torch.set_float32_matmul_precision("high")


def setup_trainer(
    val_every_n_batches: int | None,
    val_every_n_epochs: int | None,
    max_epochs: int,
) -> pl.Trainer:
    args: Dict[str, Any] = {}

    if val_every_n_batches is not None:
        assert val_every_n_epochs is None
        # Note that PL requires this be less than an epoch
        args = {**args, "val_check_interval": val_every_n_batches}
    else:
        assert val_every_n_epochs is not None
        args = {**args, "check_val_every_n_epoch": val_every_n_epochs}

    experiment_id = str(uuid.uuid1())
    dirpath = Path(PROJECT_ROOT) / "checkpoints" / experiment_id
    logging.info(f"Checkpoint will be saved in {dirpath}")
    trainer = pl.Trainer(
        enable_checkpointing=True,
        callbacks=[
            StochasticWeightAveraging(swa_lrs=1e-2),
            ModelCheckpoint(
                monitor="avg_val_collision_rate",
                save_last=False,
                auto_insert_metric_name=True,
                save_on_train_epoch_end=False,
                dirpath=dirpath,
                filename="bestval-{epoch}-{step}-{avg_val_collision_rate:.4f}",
            ),
        ],
        max_epochs=max_epochs,
        gradient_clip_val=1.0,
        accelerator="gpu",
        devices=1,
        **args,
    )
    return trainer


def parse_args_and_configuration():
    """
    Checks the command line arguments and merges them with the configuration yaml file
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("yaml_config", type=str)

    args = parser.parse_args()

    with open(args.yaml_config) as f:
        configuration = yaml.safe_load(f)

    return {
        "training_node_name": os.uname().nodename,
        **configuration,
        **vars(args),
    }


def run():
    """
    Runs the training procedure
    """
    config = parse_args_and_configuration()

    pl.utilities.rank_zero_info(f"Experiment name: {config['experiment_name']}")
    trainer = setup_trainer(
        val_every_n_batches=config.get("val_every_n_batches", None),
        val_every_n_epochs=config.get("val_every_n_epochs", None),
        max_epochs=100,
    )

    dm = DataModule(
        train_batch_size=10 if config["mintest"] else config["train_batch_size"],
        val_batch_size=10 if config["mintest"] else config["val_batch_size"],
        num_workers=(
            0 if config["mintest"] else config.get("num_workers", os.cpu_count())
        ),
        **(config["shared_parameters"] or {}),
        **(config["data_module_parameters"] or {}),
    )
    if "rope" in config and config["rope"]:
        mdl_class = ROPEMotionPolicyTransformer
    else:
        mdl_class = PretrainingMotionPolicyTransformer
    if "load_checkpoint_path" in config:
        assert "resume_training" not in config
        ckpt_path = config["load_checkpoint_path"]
        pl.utilities.rank_zero_info(f"Loading from checkpoint: {ckpt_path}")
        mdl = mdl_class.load_from_checkpoint(
            ckpt_path,
            **(config["shared_parameters"] or {}),
            **(config["training_model_parameters"] or {}),
        )
    else:
        mdl = mdl_class(
            **(config["shared_parameters"] or {}),
            **(config["training_model_parameters"] or {}),
        )

    if "resume_training" in config:
        pl.utilities.rank_zero_info(
            f"Continuing from checkpoint: {config['resume_training']['checkpoint_path']}"
        )
        trainer.fit(
            model=mdl,
            datamodule=dm,
            ckpt_path=config["resume_training"]["checkpoint_path"],
        )
    else:
        trainer.fit(model=mdl, datamodule=dm)


if __name__ == "__main__":
    run()