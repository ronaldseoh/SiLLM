import os
import time
import logging

from functools import partial

import tqdm
import numpy as np

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim

from sillm.core.llm import LLM
from sillm.models.args import ModelArgs
from sillm.training.dataset import Dataset

logger = logging.getLogger("sillm")

class TrainableLLM(LLM):
    """
    Trainable LoRA model wrapper.
    """
    @staticmethod
    def from_model(llm: LLM):
        """
        Convert LLM to trainable LLM.
        Args:
            llm: LLM to convert.
        Returns:
            Trainable LLM.
        """
        return TrainableLLM(llm.model, llm.tokenizer, llm.args)
    
    def __init__(self,
                 model,
                 tokenizer,
                 args: ModelArgs
                 ):
        """
        Args:
            tokenizer: Tokenizer instance.
            args: Model arguments.
        """
        self.model = model
        self.tokenizer = tokenizer
        self.args = args

    def loss(self, *args, **kwargs):
        """
        Default loss function from model.
        """
        return self.model.loss(*args, **kwargs)
    
    ########
    # Based on mlx-examples:
    # https://github.com/ml-explore/mlx-examples/blob/e74889d0fa0fb49d95bfdf6a1dcad907713eb50e/lora/lora.py#L198
    ########
    def evaluate(self,
                 dataset: Dataset,
                 batch_size: int,
                 num_batches: int
                 ):
        """
        Evaluate model on dataset.
        Args:
            dataset: Dataset to evaluate on.
            batch_size: Batch size.
            num_batches: Number of batches to evaluate.
        Returns:
            Average loss.
        """
        losses = []
        for _, batch in zip(
            range(num_batches),
            dataset.iterate_batches(batch_size),
        ):
            loss_value, _, _ = self.loss(*batch)
            losses.append(loss_value.item())

        return np.mean(losses)
    
    ########
    # Based on mlx-examples:
    # https://github.com/ml-explore/mlx-examples/blob/e74889d0fa0fb49d95bfdf6a1dcad907713eb50e/lora/lora.py#L212
    ########
    def train(self, 
              dataset_training: Dataset,
              dataset_validation: Dataset,
              batch_size: int = 4,
              optimizer_type: str = "adam",
              learning_rate: float = 1e-5,
              learning_decay: float = 0.0,
              compiled_step: bool = True,
              grad_checkpoint: bool = False,
              epochs: int = 1,
              iterations: int = 0,
              report_steps: int = 10,
              report_callback: callable = None,
              eval_steps: int = 100,
              eval_callback: callable = None,
              validation_samples: int = 40
              ):
        """
        Train model.
        Args:
            dataset_training: Training dataset.
            dataset_validation: Validation dataset.
            batch_size: Batch size.
            learning_rate: Learning rate.
            epochs: Number of epochs.
            iterations: Number of iterations.
            report_steps: Report every `report_steps` iterations.
            eval_steps: Evaluate every `eval_steps` iterations.
            eval_callback: Callback after eval.
            validation_samples: Number of validation samples.
            debug: Whether to enable debug mode.
        """
        # Calculate number of iterations
        if iterations == 0:
            iterations = len(dataset_training) // batch_size
        
        # Calculate number of validation batches
        validation_batches = validation_samples // batch_size
        
        logger.info(f"Training the model for {epochs} epochs of {iterations} batch iterations with batch size {batch_size}")
        logger.debug(f"Training learning rate: {learning_rate}")

        # Initialize optimizer
        optimizer_type = optimizer_type.lower()
        if optimizer_type == "adam":
            optimizer = optim.Adam(learning_rate=learning_rate)
        elif optimizer_type == "adamw":
            optimizer = optim.AdamW(learning_rate=learning_rate, weight_decay=learning_decay)
        else:
            raise ValueError(f"Unknown optimizer type: {optimizer_type}")

        if grad_checkpoint:
            logger.info(f"Enabled gradient checkpointing")

            if not compiled_step:
                logger.warning(f"Gradient checkpointing requires compiled step function")
                compiled_step = True
            
            for layer in self.model.layers:
                layer.forward = nn.utils.checkpoint(layer, layer.forward)

        # Create value and gradient function for loss
        loss_value_and_grad = nn.value_and_grad(self.model, self.loss)

        if compiled_step:
            state = [self.model.state, optimizer.state]

            # Step function for forward and backward pass
            @partial(mx.compile, inputs=state, outputs=state)
            def step(batch):
                (loss_value, reward, num_tokens), grad = loss_value_and_grad(*batch)
                optimizer.update(self.model, grad)

                return loss_value, reward, num_tokens

        # Get system memory
        system_memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")

        losses = []
        rewards = None
        intv_tokens = 0

        # Main training loop
        start = time.perf_counter()
        pbar_epochs = tqdm.tqdm(range(epochs), desc="Epoch")
        for epoch in pbar_epochs:
            pbar_iterations = tqdm.tqdm(range(iterations), desc="Iter.", leave=False)
            for iter in pbar_iterations:
                n = epoch * iterations + iter
                batch = next(dataset_training.iterate_batches(batch_size, train=True))

                if compiled_step:
                    loss_value, reward, num_tokens = step(batch)
                else:
                    (loss_value, reward, num_tokens), grad = loss_value_and_grad(*batch)
                    optimizer.update(self.model, grad)

                mx.eval(loss_value, reward, num_tokens)

                # Record loss and number of tokens
                losses.append(loss_value.item())
                intv_tokens += num_tokens.item()

                # Record rewards
                if reward is not None:
                    if rewards is None:
                        rewards = reward
                    else:
                        rewards = np.vstack([rewards, reward])

                # Get memory usage
                peak_memory = mx.metal.get_peak_memory()
                memory_usage = peak_memory / system_memory
                if memory_usage > 0.9:
                    pbar_epochs.write(f"HIGH MEMORY USAGE: {(peak_memory // (1024 ** 2)):,} MB ({memory_usage:.2%} of system memory)")
                mx.metal.reset_peak_memory()

                # Report training loss if needed
                if (n + 1) % report_steps == 0:
                    train_loss = np.mean(losses)
                    stop = time.perf_counter()

                    pbar_epochs.write(f"#{n + 1}:\tTraining loss    {train_loss:.3f}\t{float(intv_tokens) / (stop - start):.3f} tok/sec")
                    if rewards is not None:
                        pbar_epochs.write(f"#{n + 1}:\tTraining reward  {str(np.mean(rewards, axis=0))}")
                        rewards = None
                    pbar_epochs.refresh()

                    if report_callback is not None:
                        report_callback(n + 1, train_loss)
                    
                    losses = []
                    intv_tokens = 0
                    start = time.perf_counter()

                # Report validation loss if needed
                if n == 0 or (n + 1) % eval_steps == 0:
                    stop = time.perf_counter()
                    val_loss = self.evaluate(dataset_validation, batch_size, validation_batches)
                    start = time.perf_counter()
                    pbar_epochs.write(f"#{n + 1}:\tValidation loss  {val_loss:.3f}\t{(start - stop):.3f} sec")
                    pbar_epochs.write(f"#{n + 1}:\tPeak memory      {(peak_memory // (1024 ** 2)):,} MB ({memory_usage:.2%} of system memory)")

                    # Eval callback
                    if eval_callback is not None:
                        msg = eval_callback(n + 1, val_loss)
                        if msg:
                            pbar_epochs.write(f"#{n + 1}:\t" + msg)

                    start = time.perf_counter()