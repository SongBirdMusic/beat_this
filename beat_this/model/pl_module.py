""" 
    Pytorch lightning modules
"""

from typing import Any
from pathlib import Path
import torch
import torch.nn.functional as F
import numpy as np
from pytorch_lightning import LightningModule
from beat_this.model.beat_tracker import BeatThis
from beat_this.model.postprocessor import Postprocessor
import mir_eval
import torch.nn.functional as F
import beat_this.model.loss
try:
    import wandb
except ImportError:
    wandb = None
from concurrent.futures import ThreadPoolExecutor


class PLBeatThis(LightningModule):
    def __init__(
        self,
        spect_dim = 128,
        fps = 50,
        total_dim = 512,
        ff_mult = 4,
        n_layers=6,
        stem_dim=32,
        dropout=0.1,
        lr=0.0008,
        weight_decay=0.01,
        pos_weights = {"beat": 1, "downbeat": 1},
        head_dim = 32,
        loss_type = "shift_tolerant_weighted_bce",
        optimizer = 'adamw',
        warmup_steps = 1000,
        max_epochs = 100,
        use_dbn = False,
        eval_trim_beats=5,
        predict_full_pieces = False,
    ):
        super().__init__()
        self.save_hyperparameters()     
        self.lr = lr
        self.weight_decay = weight_decay
        self.fps = fps
        # create model
        self.model = BeatThis(spect_dim=spect_dim, total_dim=total_dim, ff_mult=ff_mult, stem_dim=stem_dim, n_layers=n_layers, head_dim=head_dim, dropout=dropout)
        self.optimizer = optimizer
        self.warmup_steps = warmup_steps
        self.max_epochs = max_epochs
        # set up the losses
        self.pos_weights = pos_weights
        if loss_type == "shift_tolerant_weighted_bce":
            self.beat_loss = beat_this.model.loss.ShiftTolerantBCELoss(pos_weight=pos_weights["beat"])
            self.downbeat_loss = beat_this.model.loss.ShiftTolerantBCELoss(pos_weight=pos_weights["downbeat"])
        elif loss_type == "weighted_bce":
            self.beat_loss = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weights["beat"])
            self.downbeat_loss = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weights["downbeat"])
        elif loss_type == "bce":
            self.beat_loss = torch.nn.BCEWithLogitsLoss()
            self.downbeat_loss = torch.nn.BCEWithLogitsLoss()
        else:
            raise ValueError("loss_type must be one of 'shift_tolerant_weighted_bce', 'weighted_bce', 'bce'")

        self.postprocessor = Postprocessor(type="dbn" if use_dbn else "minimal", fps=fps)
        self.eval_trim_beats = eval_trim_beats
        self.predict_full_pieces = predict_full_pieces
        self.metrics = Metrics(eval_trim_beats=eval_trim_beats)
        

    def _compute_loss(self, batch, model_prediction):
        losses = {}
        # set up the mask: a combination of the padding mask and the annotation mask (if pieces have downbeat annotations)
        mask = batch["padding_mask"] * batch["loss_mask"][:,1][:,None]
        losses["beat"] = self.beat_loss(model_prediction["beat"], batch["truth_beat"].float(), mask)
        losses["downbeat"] = self.downbeat_loss(model_prediction["downbeat"], batch["truth_downbeat"].float(), mask)
        # sum the losses
        losses["total"] = sum(losses.values())
        return losses

    def _compute_metrics(self, batch, model_prediction, step="val"):
        # compute for beat
        metrics_beat, piecewise_beat = self._compute_metrics_target(batch, model_prediction, target="beat", step=step)	
        # compute for downbeat
        metrics_downbeat, piecewise_downbeat = self._compute_metrics_target(batch,model_prediction, target="downbeat", step=step)
        
        # concatenate dictionaries
        metrics = {**metrics_beat, **metrics_downbeat}
        piecewise = {**piecewise_beat, **piecewise_downbeat}

        return metrics, piecewise
    
    def _compute_metrics_target(self, batch, model_prediction, target="beat", step="val"):  

        def compute_item(pospt_pred, truth_orig_target):
            # take the ground truth from the original version, so there are no quantization errors
            piece_truth_time = np.frombuffer(truth_orig_target)
            # run evaluation
            metrics = self.metrics(piece_truth_time, pospt_pred, step=step)
            
            return metrics, piece_truth_time

        with ThreadPoolExecutor() as executor:
            (piecewise_metrics, truth_time) = zip(*executor.map(compute_item,
                                                model_prediction[f"postp_{target}"], 
                                                batch[f"truth_orig_{target}"],
                                                ))

        
        # average the beat metrics across the dictionary
        batch_metric = {key + f"_{target}": np.mean([x[key] for x in piecewise_metrics]) for key in piecewise_metrics[0].keys()}
        # save non-averaged results for piecewise evaluation
        piecewise = {}
        if step == "test":
            piecewise[f"F-measure_{target}"] = [p["F-measure"] for p in piecewise_metrics]
            piecewise[f"CMLt_{target}"] = [p["CMLt"] for p in piecewise_metrics]
            piecewise[f"AMLt_{target}"] = [p["AMLt"] for p in piecewise_metrics]
 
        return batch_metric, piecewise

    def log_losses(self, losses, batch_size, step="train"):
        # log for separate targets and total loss
        for target in "beat", "downbeat", "total":
            self.log(f"{step}_loss_{target}", losses[target].item(), prog_bar=target=="total", on_step=False, on_epoch=True, batch_size=batch_size, sync_dist=True)
    
    def log_metrics(self, metrics, batch_size, step="val"):
        for key, value in metrics.items():
            self.log(f"{step}_{key}", value, prog_bar=False, on_step=False, on_epoch=True, batch_size=batch_size, sync_dist=True)

    def training_step(self, batch, batch_idx):
        # run the model
        model_prediction = self.model(batch["spect"])
        # compute loss
        losses = self._compute_loss(batch, model_prediction)
        self.log_losses(losses, len(batch["spect"]), "train")
        return losses["total"]

    def validation_step(self, batch, batch_idx):
        # run the model
        model_prediction = self.model(batch["spect"])
        # compute loss
        losses = self._compute_loss(batch, model_prediction)
        # postprocess the predictions
        model_prediction = self.postprocessor(model_prediction, batch["padding_mask"])
        # compute the metrics
        metrics, piecewise = self._compute_metrics(batch, model_prediction, step="val")
        # log
        self.log_losses(losses, len(batch["spect"]), "val")
        self.log_metrics(metrics, batch["spect"].shape[0], "val")

    def test_step(self, batch, batch_idx):
        # run the model
        model_prediction = self.model(batch["spect"])
        # compute loss and slow metrics
        shared_out, metrics = self._compute_metrics(batch, model_prediction, step="test")
        # log
        self.log_losses(shared_out, len(batch["spect"]), "test")
        self.log_slow_metrics(metrics, batch["spect"].shape[0], "test")

    # def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0, overlap: int = 0, overlaps: str = 'keep_first') -> Any:
    #     """
    #     Compute predictions and metrics for a batch (a dictionary with an "audio" key).
    #     If self.predict_full_pieces is true-ish, will split up the audio into multiple excerpts.
    #     Potential overlaps between excerpts can be handled by averaging them (overlaps='average'),
    #     by keeping the predictions of the excerpt coming first (overlaps='keep_first'), or
    #     by keeping the predictions of the excerpt coming last (overlaps='keep_last').
    #     Note that overlaps appear even when overlap=0 as the last excerpt is moved backwards
    #     when it would extend over the end of the piece.
    #     """
    #     if self.predict_full_pieces:
    #         if batch["audio"].shape[0] != 1:
    #             raise ValueError("When `predict_full_pieces` is True, only `batch_size=1` is supported")
    #         if torch.any(~batch["padding_mask"]):
    #             raise ValueError("When `predict_full_pieces` is True, the Dataset must not pad inputs")
    #         frames_to_audio = (1 if self.input_enc[0].startswith('dac') else self.hop_size)
    #         # split up the audio into chunks
    #         audio = batch["audio"][0]
    #         chunk_size = self.max_length * frames_to_audio
    #         assert self.num_lost_frames % 2 == 0
    #         border_size = self.num_lost_frames // 2 * frames_to_audio
    #         chunks, starts = split_piece(audio, chunk_size, border_size, overlap=overlap, avoid_short_end=True)
    #         # run the model
    #         outputs = [self.module(chunk.unsqueeze(0)) for chunk in chunks]
    #         # aggregate the predictions for the whole piece
    #         if self.input_enc[0].startswith('dac'):
    #             total_length = len(audio)
    #         else:
    #             total_length = (len(audio) + self.hop_size - 1) // self.hop_size
    #         piece_prediction_beat = torch.full((total_length,), -1000., device=audio.device)
    #         piece_prediction_downbeat = torch.full((total_length,), -1000., device=audio.device)
    #         if overlaps == 'average':
    #             number_of_predictions = torch.zeros(total_length, device=audio.device)
    #         excerpts = zip(starts, outputs)
    #         if overlaps == 'keep_first':
    #             # process in reverse order, so predictions of earlier excerpts overwrite later ones
    #             excerpts = reversed(list(excerpts))
    #         for start, output in excerpts:
    #             # add the predictions and take note of overlaps
    #             start //= frames_to_audio
    #             end = min(start + output["beat"].shape[1], total_length)
    #             piece_prediction_beat[start:end] = output["beat"][0][:end - start]
    #             piece_prediction_downbeat[start:end] = output["downbeat"][0][:end - start]
    #             if overlaps == 'average':
    #                 number_of_predictions[start:end] += 1
    #         if overlaps == 'average':
    #             # clamp the number of predictions to avoid division by zero. This should not be necessary, but it is
    #             number_of_predictions = torch.clamp(number_of_predictions, min=1)
    #             # normalize if multiple predictions at the same place
    #             piece_prediction_beat = piece_prediction_beat / number_of_predictions
    #             piece_prediction_downbeat = piece_prediction_downbeat / number_of_predictions
    #         # save it to model_prediction
    #         model_prediction = {}
    #         model_prediction["beat"] = piece_prediction_beat.unsqueeze(0)
    #         model_prediction["downbeat"] = piece_prediction_downbeat.unsqueeze(0)
    #     else:
    #         # run the model
    #         model_prediction = self.module(batch["audio"], batch["padding_mask"])

    #     shared_out, metrics = self._compute_metrics(batch, model_prediction, step="test")
    #     metadata = dict(audio_path=batch["audio_path"])
    #     return dict(ChainMap(shared_out, metrics, metadata))


    def configure_optimizers(self):
        optimizer = torch.optim.AdamW
        # only decay 2+-dimensional tensors, to exclude biases and norms
        # (filtering on dimensionality idea taken from Kaparthy's nano-GPT)
        params = [{'params': (p for p in self.parameters()
                                if p.requires_grad and p.ndim >= 2),
                    'weight_decay': self.weight_decay},
                    {'params': (p for p in self.parameters()
                                if p.requires_grad and p.ndim <= 1),
                    'weight_decay': 0}]

        optimizer = optimizer(params, lr=self.lr)

        self.lr_scheduler = CosineWarmupScheduler(optimizer, self.warmup_steps, self.trainer.estimated_stepping_batches)

        result = dict(optimizer=optimizer)
        result['lr_scheduler'] = {"scheduler": self.lr_scheduler, "interval": "step"}
        return result


class Metrics:
    def __init__(self, eval_trim_beats : int) -> None:
        self.min_beat_time = eval_trim_beats
        
    def __call__(self, truth, preds, step) -> Any:
        truth = mir_eval.beat.trim_beats(truth, min_beat_time=self.min_beat_time)
        preds = mir_eval.beat.trim_beats(preds, min_beat_time=self.min_beat_time)
        if step == "val": # limit the metrics that are computed during validation to speed up training
            fmeasure =  mir_eval.beat.f_measure(truth, preds)
            cemgil = mir_eval.beat.cemgil(truth, preds)
            return {'F-measure':fmeasure, "Cemgil":cemgil}
        elif step == "test": # compute all metrics during testing
            CMLc, CMLt, AMLc, AMLt = mir_eval.beat.continuity(truth, preds)
            fmeasure =  mir_eval.beat.f_measure(truth, preds)
            cemgil = mir_eval.beat.cemgil(truth, preds)
            return {'F-measure':fmeasure, "Cemgil":cemgil, "CMLt":CMLt, "AMLt":AMLt}
        else:
            raise ValueError("step must be either val or test")
        

class CosineWarmupScheduler(torch.optim.lr_scheduler._LRScheduler):
    """
    Cosine annealing over `max_iters` steps with `warmup` linear warmup steps.
    Optionally re-raises the learning rate for the final `raise_last` fraction
    of total training time to `raise_to` of the full learning rate, again with
    a linear warmup (useful for stochastic weight averaging).
    """
    def __init__(self, optimizer, warmup, max_iters, raise_last=0, raise_to=.5):
        self.warmup = warmup
        self.max_num_iters = int((1 - raise_last) * max_iters)
        self.raise_to = raise_to
        super().__init__(optimizer)

    def get_lr(self):
        lr_factor = self.get_lr_factor(step=self.last_epoch)
        return [base_lr * lr_factor for base_lr in self.base_lrs]

    def get_lr_factor(self, step):
        if step < self.max_num_iters:
            progress = step / self.max_num_iters
            lr_factor = 0.5 * (1 + np.cos(np.pi * progress))
            if step <= self.warmup:
                lr_factor *= step / self.warmup
        else:
            progress = (step - self.max_num_iters) / self.warmup
            lr_factor = self.raise_to * min(progress, 1)
        return lr_factor
