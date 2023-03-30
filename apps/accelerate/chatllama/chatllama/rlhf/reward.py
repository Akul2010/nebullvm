import json


import torch
from beartype import beartype
from torch.utils.data import Dataset, DataLoader

from chatllama.rlhf.base_model import BaseModel, BaseTrainer
from chatllama.rlhf.config import ConfigReward
from chatllama.rlhf.dataset import BaseDataset


class RewardModel(BaseModel):
    """Model to be trained to predict the reward for RL.
    or to be used as Critic in RL. It is a Language Model with a head
    that predicts the reward (a scalar) for a given sequence of tokens.

    Methods:
        forward: Forward pass of the model (used by the critic)
        get_reward: Get the reward for a given input (used by the reward model)

    """

    def __init__(self, config: ConfigReward) -> None:
        super().__init__(config)

    @beartype
    def forward(
        self, output_sequence: torch.Tensor, output_sequence_mask: torch.Tensor
    ) -> torch.Tensor:
        """Generate the sequence of rewards for the given output sequence
        what is the quality of the output sequence tokens?

        Args:
            output_sequence (torch.Tensor): The sequence of tokens to be
                evaluated
            output_sequence_mask (torch.Tensor): Mask for the attention

        Returns:
            torch.Tensor: Rewards for the given output sequence
        """
        output = self.model.forward(
            output_sequence, attention_mask=output_sequence_mask,
        )

        # What if the output_sequence is longer than the max context of
        # the model?
        rewards = self.head(output.last_hidden_state)
        if self.config.debug:
            print("RewardModel.forward")
            print("output_sequence.shape", output_sequence.shape)
            print("output_sequence", output_sequence)
            print("reward.shape", rewards.shape)
            print("reward", rewards)
        return rewards

    @beartype
    def get_reward(
        self, output_sequence: torch.Tensor, output_sequence_mask: torch.Tensor
    ) -> torch.Tensor:
        """Get the reward for the given output sequence

        Args:
            output_sequence (torch.Tensor): The concatenation of initial input
                and actor output as tokens
            output_sequence_mask (torch.Tensor): Mask for the attention
        """
        if output_sequence.shape[1] > self.config.max_sequence_length:
            raise ValueError(
                f"Output sequence is too long: {output_sequence.shape[1]}"
                f" > {self.config.max_sequence_length}"
            )
        rewards = self.forward(output_sequence, output_sequence_mask)
        return rewards[:, -1]


# just to keep namings consistent
CriticModel = RewardModel


class RewardDataset(Dataset):
    """Dataset class for the reward model
    read a json file with the following format:
    [
        {
            "user_input": "...",
            "completion": "...",
            "score": ...
        },
        ...
    ]
    Where:
        user_input: the initial input of the user
        completion: the completion generated by the model
        score: the score given by the user to the completion (or by the LLM)
    """

    def __init__(self, path: str) -> None:
        with open(path, "r") as f:
            self.data = list(json.load(f))

    def __getitem__(self, idx: int):
        user_input = self.data[idx]["user_input"]
        completion = self.data[idx]["completion"]
        score = float(self.data[idx]["score"])
        item = (user_input + completion, score)
        return item

    def __len__(
        self,
    ):
        return len(self.data)


class RewardTrainer(BaseTrainer):
    """Class to train the reward model

    Args:
        config (ConfigModel): Config parameters for the model

    Attributes:
        model (RewardModel): Reward model
        config (ConfigModel): Config parameters for the model
        optimizer (torch.optim): Optimizer for the model
        loss_function (torch.nn): Loss function for the model
        validation_flag (bool): Flag to indicate if the validation dataset
            is available
        train_dataset (RewardDataset): Dataset for training
        validation_dataset (RewardDataset): Dataset for validation
        train_dataloader (DataLoader): Dataloader for training
        validation_dataloader (DataLoader): Dataloader for validation
        scheduler (torch.optim.lr_scheduler): Scheduler for the optimizer
        training_stats (List[Dict]): List of dictionaries with the training
            statistics
        model_engine (ModelEngine): Model engine to train the model
            using deepspeed
        accelerator (Accelerator): Accelerator to train the model using
            accelerate by HF.


    Methods:
        train: Train the reward model
    """

    def __init__(self, config: ConfigReward) -> None:

        super().__init__(config)

        # load the model
        self.model = RewardModel(config)

        # optimizer
        if self.deepspeed_enable:
            import deepspeed
            deepspeed.ops.op_builder.CPUAdamBuilder().load()
            self.optimizer = deepspeed.ops.adam.DeepSpeedCPUAdam(
                self.model.parameters(), lr=config.lr
            )
        else:
            self.optimizer = torch.optim.AdamW(
                self.model.parameters(), lr=config.lr
            )

        # loss function
        self.loss_function = torch.nn.MSELoss()

        # check validation dataset
        self.validation_flag = False
        if config.validation_dataset_path is not None:
            self.validation_flag = True

        # create dataset and dataloaders
        BaseDataset.clean_dataset(config)
        self.train_dataset = RewardDataset(config.train_dataset_path)
        self.train_dataloader = DataLoader(
            self.train_dataset, batch_size=config.batch_size
        )
        if self.validation_flag:
            BaseDataset.clean_dataset(config)
            self.eval_dataset = RewardDataset(config.validation_dataset_path)
            self.validation_dataloader = DataLoader(
                self.eval_dataset, batch_size=config.batch_size
            )

        # intilize scheduler - learning rate will drop to 10% of the initial
        # value
        self.scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            self.optimizer,
            T_0=len(self.train_dataset) // config.batch_size,
            T_mult=1,
            eta_min=config.lr * 0.1,
            last_epoch=-1,
        )

        # deepspeed
        self.setup_deepspeed()

        # HF accelerate
        self.setup_accelerate()

    def train(
        self,
    ) -> None:
        """Train the reward model"""
        
        self.logger.success("Start Training the Reward Model")
        
        # setup the logs 
        self.setup_logs()

        # get config parameters
        if self.config.deepspeed_enable:
            batch_size = self.train_dataloader.batch_size
        else:
            batch_size = self.config.batch_size

        epochs = self.config.epochs
        device = self.config.device
        iteration_per_print = self.config.iteration_per_print
        checkpoint_steps = self.config.checkpoint_steps

        # compute the number of iterations
        n_iter = int(len(self.train_dataset) / batch_size)

        # load checkpoint
        start_epoch, start_step = self.load_checkpoint()

        # counter for the checkpoint
        cnt_checkpoints = 1

        # traing loop
        for epoch in range(start_epoch, epochs):
            self.model.train()
            for i, inputs in enumerate(self.train_dataloader):

                # skip the steps if resuming from a checkpoint
                if i < start_step:
                    continue

                # get the inputs
                input_text = inputs[0]
                score = inputs[1]

                # tokenize the input
                with torch.no_grad():
                    input_tokens = self.model.tokenizer(
                        input_text,
                        return_tensors="pt",
                        truncation=True,
                        padding=True,
                    )
                    output = torch.as_tensor(
                        score, dtype=torch.float32, device=device
                    )

                # forward pass
                if self.config.deepspeed_enable:
                    est_output = self.model_engine(
                        input_tokens["input_ids"].to(device),
                        input_tokens["attention_mask"].to(device),
                    )[:, -1]
                else:
                    est_output = self.model.get_reward(
                        input_tokens["input_ids"].to(device),
                        input_tokens["attention_mask"].to(device),
                    )

                # compute the loss
                loss = self.loss_function(est_output, output)
                self.append_training_stats(training_loss=loss.item())

                # backward pass
                if self.config.deepspeed_enable:
                    self.model_engine.backward(loss)
                    self.model_engine.step()
                elif self.config.accelerate_enable:
                    self.optimizer.zero_grad()
                    self.accelerator.backward(loss)
                    self.optimizer.step()
                    self.scheduler.step()
                else:
                    self.optimizer.zero_grad()
                    loss.backward()
                    self.optimizer.step()
                    self.scheduler.step()

                # print progress
                if i % iteration_per_print == 0:
                    self.logger.info(
                        f"Epoch: {epoch+1}/{epochs}, "
                        f"Iteration: {i+1}/{n_iter}, "
                        f"Training Loss: {loss.item()}"
                    )
                    printed_est_output = [
                        round(float(x), 1) for x in est_output.cpu().tolist()
                    ]
                    self.logger.info(
                        f"prediction {printed_est_output} "
                        f"target {score.cpu().tolist()}"
                    )

                # checkpoints saving
                if cnt_checkpoints % checkpoint_steps == 0:
                    self.save_checkpoint(epoch, i, epochs, n_iter)
                    cnt_checkpoints = 1
                else:
                    cnt_checkpoints += 1

            # Validation
            if self.validation_flag:
                self.model.eval()
                with torch.no_grad():
                    for i, (text, score) in enumerate(
                        self.validation_dataloader
                    ):

                        # tokenize inputs
                        input_tokens = self.model.tokenizer(
                            text, return_tensors="pt", padding=True
                        )
                        input_tokens = input_tokens.to(device)
                        # TODO: check on the length of the input tokens if
                        # they are too many it can create problems
                        output = torch.tensor(score, dtype=torch.float32).to(
                            device
                        )

                        # forward pass
                        est_output = self.model.get_reward(
                            input_tokens["input_ids"],
                            input_tokens["attention_mask"],
                        )

                        # compute loss
                        loss = self.loss_function(est_output, output)
                        self.append_training_stats(validation_loss=loss.item())

                        # print progress
                        if i % iteration_per_print == 0:
                            self.logger.info(
                                f"Epoch: {epoch+1}/{epochs}, "
                                f"Iteration: {i+1}/{n_iter}, "
                                f"Validation Loss: {loss.item()}"
                            )
            # reset start_step after training is resumed
            start_step = 0

        # save the model at the end of the training
        self.model.save()
        self.logger.success("Training is finished")
