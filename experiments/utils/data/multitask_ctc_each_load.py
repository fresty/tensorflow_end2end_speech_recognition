#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Load dataset for the multitask CTC model.
   You can use the multi-GPU version.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import basename
import random
import numpy as np
import tensorflow as tf

from experiments.utils.data.frame_stack import stack_frame
from experiments.utils.sparsetensor import list2sparsetensor


class DatasetBase(object):

    def __init__(self, data_type, label_type_main, label_type_sub,
                 batch_size, num_stack=None, num_skip=None,
                 is_sorted=True, is_progressbar=False, num_gpu=1):
        """Load mini-batch in each step.
        Args:
            data_type: string
            label_type_main: string
            label_type_sub: string
            batch_size: int, the size of mini-batch
            num_stack: int, the number of frames to stack
            num_skip: int, the number of frames to skip
            is_sorted: if True, sort dataset by frame num
            is_progressbar: if True, visualize progressbar
            num_gpu: int, if more than 1, divide batch_size by num_gpu
        """
        self.data_type = data_type
        self.label_type_main = label_type_main
        self.label_type_sub = label_type_sub
        self.batch_size = batch_size * num_gpu
        self.is_sorted = is_sorted
        self.is_progressbar = is_progressbar
        self.num_gpu = num_gpu

        self.input_size = None

        # Step
        # 1. Load the frame number dictionary
        self.frame_num_dict = None

        # 2. Load all paths to input & label
        self.input_paths = None
        self.label_main_paths = None
        self.label_sub_paths = None
        self.data_num = None

        # 3. Load all dataset in advance
        self.input_list = None
        self.label_main_list = None
        self.label_sub_list = None
        self.rest = set(range(0, self.data_num, 1))

    def next_batch(self, batch_size=None, session=None):
        """Make mini-batch.
        Args:
            batch_size: int, the size of mini-batch
            session:
        Returns:
            inputs: list of input data, size `[batch_size]`
            labels_main: list of target labels in the main task
            labels_sub: list of target labels in the sub task
            inputs_seq_len: list of length of inputs of size `[batch_size]`
            input_names: list of file name of input data of size `[batch_size]`

            If num_gpu > 1, each return is divide into list of size `[num_gpu]`
        """
        if session is None and self.num_gpu != 1:
            raise ValueError('Set session when using multiple GPUs.')

        if batch_size is None:
            batch_size = self.batch_size

        next_epoch_flag = False
        padded_value = -1

        while True:
            # sorted dataset
            if self.is_sorted:
                if len(self.rest) > batch_size:
                    data_indices = list(self.rest)[:batch_size]
                    self.rest -= set(data_indices)
                else:
                    data_indices = list(self.rest)
                    self.rest = set(range(0, self.data_num, 1))
                    next_epoch_flag = True
                    if self.data_type == 'train':
                        print('---Next epoch---')

                # Shuffle selected mini-batch
                random.shuffle(data_indices)

            # not sorted dataset
            else:
                if len(self.rest) > batch_size:
                    # Randomly sample mini-batch
                    data_indices = random.sample(
                        list(self.rest), batch_size)
                    self.rest -= set(data_indices)
                else:
                    data_indices = list(self.rest)
                    self.rest = set(range(0, self.data_num, 1))
                    next_epoch_flag = True
                    if self.data_type == 'train':
                        print('---Next epoch---')

                    # Shuffle selected mini-batch
                    random.shuffle(data_indices)

            # Load dataset in mini-batch
            input_list = np.array(list(
                map(lambda path: np.load(path),
                    np.take(self.input_paths, data_indices, axis=0))))
            label_main_list = np.array(list(
                map(lambda path: np.load(path),
                    np.take(self.label_main_paths, data_indices,
                            axis=0))))
            label_sub_list = np.array(list(
                map(lambda path: np.load(path),
                    np.take(self.label_sub_paths, data_indices,
                            axis=0))))
            input_names = list(
                map(lambda path: basename(path).split('.')[0],
                    np.take(self.input_paths, data_indices, axis=0)))

            # Frame stacking
            if not ((self.num_stack is None) or (self.num_skip is None)):
                input_list = stack_frame(
                    input_list,
                    self.input_paths[data_indices],
                    self.frame_num_dict,
                    self.num_stack,
                    self.num_skip,
                    is_progressbar=False)

            # Compute max frame num in mini-batch
            max_frame_num = max(map(lambda x: x.shape[0], input_list))

            # Compute max target label length in mini-batch
            max_seq_len_main = max(map(len, label_main_list))
            max_seq_len_sub = max(map(len, label_sub_list))

            # Initialization
            inputs = np.zeros(
                (len(data_indices), max_frame_num, self.input_size),
                dtype=np.float32)
            if not self.is_test:
                labels_main = np.array(
                    [[padded_value] * max_seq_len_main]
                    * len(data_indices), dtype=np.int32)
                labels_sub = np.array(
                    [[padded_value] * max_seq_len_sub]
                    * len(data_indices), dtype=np.int32)
            else:
                labels_main = [None] * len(data_indices)
                labels_sub = [None] * len(data_indices)
            inputs_seq_len = np.empty(
                (len(data_indices),), dtype=np.int32)

            # Set values of each data in mini-batch
            for i_batch in range(len(data_indices)):
                data_i = input_list[i_batch]
                frame_num = data_i.shape[0]
                inputs[i_batch, :frame_num, :] = data_i
                if not self.is_test:
                    labels_main[i_batch, :len(
                        label_main_list[i_batch])] = label_main_list[i_batch]
                    labels_sub[i_batch, :len(
                        label_sub_list[i_batch])] = label_sub_list[i_batch]
                else:
                    labels_main[i_batch] = label_main_list[i_batch]
                    labels_sub[i_batch] = label_sub_list[i_batch]
                inputs_seq_len[i_batch] = frame_num

            ##########
            # GPU
            ##########
            if self.num_gpu > 1:
                divide_num = self.num_gpu
                if next_epoch_flag:
                    for i in range(self.num_gpu, 0, -1):
                        if len(self.rest) % i == 0:
                            divide_num = i
                            break
                    next_epoch_flag = False

                # Now we split the mini-batch data by num_gpu
                inputs = tf.split(inputs, divide_num, axis=0)
                labels_main = tf.split(labels_main, divide_num, axis=0)
                labels_sub = tf.split(labels_sub, divide_num, axis=0)
                inputs_seq_len = tf.split(inputs_seq_len, divide_num, axis=0)
                input_names = tf.split(input_names, divide_num, axis=0)

                # Convert from SparseTensor to numpy.ndarray
                inputs = list(map(session.run, inputs))
                labels_main = list(map(session.run, labels_main))
                labels_sub = list(map(session.run, labels_sub))
                inputs_seq_len = list(map(session.run, inputs_seq_len))
                input_names = list(map(session.run, input_names))

            yield (inputs, labels_main, labels_sub, inputs_seq_len,
                   input_names)
