
# pylint: disable=too-many-statements
"""
Training and evaluation of a neural network which predicts 3D localization and confidence intervals
given 2d joints
"""

import copy
import os
import datetime
import logging
from collections import defaultdict
import sys
import time
import warnings
import math

import matplotlib
matplotlib.use('module://backend_interagg')
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler

from .datasets import KeypointsDataset
from ..network import LaplacianLoss
from ..network.process import unnormalize_bi
from ..network.architectures import LinearModel
from ..utils import set_logger


class Trainer:
    def __init__(self, joints, epochs=100, bs=256, dropout=0.2, lr=0.002,
                 sched_step=20, sched_gamma=1, hidden_size=256, n_stage=3, r_seed=1, n_samples=100,
                 baseline=False, save=False, print_loss=True):
        """
        Initialize directories, load the data and parameters for the training
        """

        # Initialize directories and parameters
        dir_out = os.path.join('data', 'models')
        if not os.path.exists(dir_out):
            warnings.warn("Warning: output directory not found, the model will not be saved")
        dir_logs = os.path.join('data', 'logs')
        if not os.path.exists(dir_logs):
            warnings.warn("Warning: default logs directory not found")
        assert os.path.exists(joints), "Input file not found"

        self.joints = joints
        self.num_epochs = epochs
        self.save = save
        self.print_loss = print_loss
        self.baseline = baseline
        self.lr = lr
        self.sched_step = sched_step
        self.sched_gamma = sched_gamma
        n_joints = 17
        input_size = n_joints * 2
        self.output_size = 2
        self.clusters = ['10', '20', '30', '>30']
        self.hidden_size = hidden_size
        self.n_stage = n_stage
        self.dir_out = dir_out
        self.n_samples = n_samples
        self.r_seed = r_seed
        now = datetime.datetime.now()
        now_time = now.strftime("%Y%m%d-%H%M")[2:]
        name_out = 'monoloco-' + now_time

        # Loss functions
        self.l_loc = LaplacianLoss().cuda()
        self.l_orient = nn.L1Loss().cuda()
        self.l_eval = nn.L1Loss().cuda()

        if self.save:
            self.path_model = os.path.join(dir_out, name_out + '.pkl')
            self.logger = set_logger(os.path.join(dir_logs, name_out))
            self.logger.info("Training arguments: \nepochs: {} \nbatch_size: {} \ndropout: {}"
                             "\nbaseline: {} \nlearning rate: {} \nscheduler step: {} \nscheduler gamma: {}  "
                             "\ninput_size: {} \nhidden_size: {} \nn_stages: {} \nr_seed: {}"
                             "\ninput_file: {}"
                             .format(epochs, bs, dropout, baseline, lr, sched_step, sched_gamma, input_size,
                                     hidden_size, n_stage, r_seed, self.joints))
        else:
            logging.basicConfig(level=logging.INFO)
            self.logger = logging.getLogger(__name__)

        # Select the device and load the data
        use_cuda = torch.cuda.is_available()
        self.device = torch.device("cuda:1" if use_cuda else "cpu")
        print('Device: ', self.device)

        # Set the seed for random initialization
        torch.manual_seed(r_seed)
        if use_cuda:
            torch.cuda.manual_seed(r_seed)

        # Dataloader
        self.dataloaders = {phase: DataLoader(KeypointsDataset(self.joints, phase=phase),
                                              batch_size=bs, shuffle=True) for phase in ['train', 'val']}

        self.dataset_sizes = {phase: len(KeypointsDataset(self.joints, phase=phase))
                              for phase in ['train', 'val']}

        # Define the model
        self.logger.info('Sizes of the dataset: {}'.format(self.dataset_sizes))
        print(">>> creating model")
        self.model = LinearModel(input_size=input_size, output_size=self.output_size, linear_size=hidden_size,
                                 p_dropout=dropout, num_stage=self.n_stage)
        self.model.to(self.device)
        print(">>> total params: {:.2f}M".format(sum(p.numel() for p in self.model.parameters()) / 1000000.0))

        # Optimizer and scheduler
        self.optimizer = torch.optim.Adam(params=self.model.parameters(), lr=lr)
        self.scheduler = lr_scheduler.StepLR(self.optimizer, step_size=self.sched_step, gamma=self.sched_gamma)

    def train(self):

        # Initialize the variable containing model weights
        since = time.time()
        best_model_wts = copy.deepcopy(self.model.state_dict())
        best_acc = 1e6
        best_training_acc = 1e6
        best_epoch = 0
        epoch_losses_tr = []
        epoch_losses_val = []

        for epoch in range(self.num_epochs):

            # Each epoch has a training and validation phase
            for phase in ['train', 'val']:
                if phase == 'train':
                    running_loss_tr = 0.
                    self.scheduler.step()
                    self.model.train()  # Set model to training mode
                else:
                    running_loss_eval = 0.
                    self.model.eval()  # Set model to evaluate mode

                # Iterate over data.
                for inputs, labels, _, _ in self.dataloaders[phase]:
                    inputs = inputs.to(self.device)
                    labels = labels.to(self.device)
                    gt_loc = labels[:, 0:1]
                    gt_orient = labels[:, 1:3]

                    # zero the parameter gradients
                    self.optimizer.zero_grad()

                    # forward
                    # track history if only in train
                    with torch.set_grad_enabled(phase == 'train'):
                        outputs = self.model(inputs)
                        # loc = outputs[:, 0:2]
                        # orient = outputs[:, 2:3]
                        orient = outputs

                        loss = self.l_orient(orient, gt_orient)
                        if loss.item() < 0.4:
                            aa = 5
                        # loss_eval = self.l_eval(orient, gt_orient)  # L1 loss to evaluation

                        # backward + optimize only if in training phase
                        aa = 5
                        if phase == 'train':
                            loss.backward()
                            self.optimizer.step()
                            running_loss_tr += get_angle_loss(orient, gt_orient) * inputs.size(0)
                        else:
                            running_loss_eval += get_angle_loss(orient, gt_orient) * inputs.size(0)

            training_acc = running_loss_tr / self.dataset_sizes['train']
            val_acc = running_loss_eval / self.dataset_sizes['val']
            epoch_losses_tr.append(training_acc)
            epoch_losses_val.append(val_acc)

            if epoch % 5 == 1:
                sys.stdout.write('\r' + 'Epoch: {:.0f}   Training Loss: {:.3f}   Val Loss {:.3f}'
                                 .format(epoch, epoch_losses_tr[-1], epoch_losses_val[-1]) + '\t')

            # deep copy the model
            if val_acc < best_acc:
                best_acc = val_acc
                best_training_acc = training_acc
                best_epoch = epoch
                best_model_wts = copy.deepcopy(self.model.state_dict())

        time_elapsed = time.time() - since
        print('\n\n' + '-'*120)
        self.logger.info('Training:\nTraining complete in {:.0f}m {:.0f}s'
                         .format(time_elapsed // 60, time_elapsed % 60))
        self.logger.info('Best training Accuracy: {:.3f}'.format(best_training_acc))
        self.logger.info('Best validation Accuracy: {:.3f}'.format(best_acc))
        self.logger.info('Saved weights of the model at epoch: {}'.format(best_epoch))

        if self.print_loss:
            epoch_losses_val_scaled = epoch_losses_val  # to compare with L1 Loss
            plt.plot(epoch_losses_tr[10:], label='Training Loss')
            plt.plot(epoch_losses_val_scaled[10:], label='Validation Loss')
            plt.legend()
            plt.savefig('loss.png')

        # load best model weights
        self.model.load_state_dict(best_model_wts)

        return best_epoch

    def evaluate(self, load=False, model=None, debug=False):

        # To load a model instead of using the trained one
        if load:
            self.model.load_state_dict(torch.load(model, map_location=lambda storage, loc: storage))

        # Average distance on training and test set after unnormalizing
        self.model.eval()
        dic_err = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: 0)))  # initialized to zero
        phase = 'val'
        batch_size = 5000
        dataset = KeypointsDataset(self.joints, phase=phase)
        size_eval = len(dataset)
        start = 0
        with torch.no_grad():
            for end in range(batch_size, size_eval+batch_size, batch_size):
                end = end if end < size_eval else size_eval
                inputs, labels, _, _ = dataset[start:end]
                start = end
                inputs = inputs.to(self.device)
                labels = labels.to(self.device)
                gt_orient = labels[:, 1:2]

                # Debug plot for input-output distributions
                if debug:
                    debug_plots(inputs, labels)
                    sys.exit()

                # Forward pass
                outputs = self.model(inputs)
                # outputs = unnormalize_bi(outputs)

                dic_err[phase]['all'] = self.compute_stats(outputs, gt_orient, dic_err[phase]['all'], size_eval)

            print('-'*120)
            self.logger.info("Evaluation:\nAverage distance on the {} set: {:.2f}"
                             .format(phase, dic_err[phase]['all']['mean']))

            self.logger.info("Aleatoric Uncertainty: {:.2f}, inside the interval: {:.1f}%\n"
                             .format(dic_err[phase]['all']['bi'], dic_err[phase]['all']['conf_bi']*100))

            # Evaluate performances on different clusters and save statistics
            for clst in self.clusters:
                inputs, labels, size_eval = dataset.get_cluster_annotations(clst)
                inputs, labels = inputs.to(self.device), labels.to(self.device)

                # Forward pass on each cluster
                outputs = self.model(inputs)
                # outputs = unnormalize_bi(outputs)

                dic_err[phase][clst] = self.compute_stats(outputs, labels, dic_err[phase][clst], size_eval)

                self.logger.info("{} error in cluster {} = {:.2f} for {} instances. "
                                 "Aleatoric of {:.2f} with {:.1f}% inside the interval"
                                 .format(phase, clst, dic_err[phase][clst]['mean'], size_eval,
                                         dic_err[phase][clst]['bi'], dic_err[phase][clst]['conf_bi'] * 100))

        # Save the model and the results
        if self.save and not load:
            torch.save(self.model.state_dict(), self.path_model)
            print('-'*120)
            self.logger.info("\nmodel saved: {} \n".format(self.path_model))
        else:
            self.logger.info("\nmodel not saved\n")

        return dic_err, self.model

    def compute_stats(self, outputs, labels_orig, dic_err, size_eval):
        """Compute mean, bi and max of torch tensors"""

        labels = labels_orig.view(-1, )
        mean_mu = float(self.l_eval(outputs[:, 0], labels).item())
        max_mu = float(torch.max(torch.abs((outputs[:, 0] - labels))).item())

        if self.baseline:
            return (mean_mu, max_mu), (0, 0, 0)

        # mean_bi = torch.mean(outputs[:, 1]).item()

        # low_bound_bi = labels >= (outputs[:, 0] - outputs[:, 1])
        # up_bound_bi = labels <= (outputs[:, 0] + outputs[:, 1])
        # bools_bi = low_bound_bi & up_bound_bi
        # conf_bi = float(torch.sum(bools_bi)) / float(bools_bi.shape[0])

        dic_err['mean'] += mean_mu * (outputs.size(0) / size_eval)
        # dic_err['bi'] += mean_bi * (outputs.size(0) / size_eval)
        dic_err['bi'] = 0
        dic_err['count'] += (outputs.size(0) / size_eval)
        # dic_err['conf_bi'] += conf_bi * (outputs.size(0) / size_eval)
        dic_err['conf_bi'] = 0
        return dic_err


def debug_plots(inputs, labels):
    inputs_shoulder = inputs.cpu().numpy()[:, 5]
    inputs_hip = inputs.cpu().numpy()[:, 11]
    labels = labels.cpu().numpy()
    heights = inputs_hip - inputs_shoulder
    plt.figure(1)
    plt.hist(heights, bins='auto')
    plt.show()
    plt.figure(2)
    plt.hist(labels, bins='auto')
    plt.show()


def get_angle_loss(orient, gt_orient):
    angle = torch.atan2(orient[:, 0], orient[:, 1]) * 180 / math.pi
    gt_angle = torch.atan2(gt_orient[:, 0], gt_orient[:, 1]) * 180 / math.pi
    angle_loss = torch.mean(torch.abs(angle - gt_angle))
    return angle_loss

