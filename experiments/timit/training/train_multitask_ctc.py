#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Train the multi-task CTC model (TIMIT corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import join, isfile
import sys
import time
import tensorflow as tf
from setproctitle import setproctitle
import yaml
import shutil

sys.path.append('../../../')
from experiments.timit.data.load_dataset_multitask_ctc import Dataset
from experiments.timit.metrics.ctc import do_eval_per, do_eval_cer
from experiments.utils.directory import mkdir, mkdir_join
from experiments.utils.parameter import count_total_parameters
from experiments.utils.csv import save_loss, save_ler
from experiments.utils.sparsetensor import list2sparsetensor
from models.ctc.load_model_multitask import load


def do_train(network, param):
    """Run multi-task training. The target labels in the main task is
    characters and those in the sub task is 61 phones. The model is
    evaluated by CER and PER with 39 phones.
    Args:
        network: network to train
        param: A dictionary of parameters
    """
    # Load dataset
    train_data = Dataset(data_type='train',
                         label_type_main='character',
                         label_type_sub=param['label_type_sub'],
                         batch_size=param['batch_size'],
                         num_stack=param['num_stack'],
                         num_skip=param['num_skip'],
                         is_sorted=True)
    dev_data = Dataset(data_type='dev',
                       label_type_main='character',
                       label_type_sub=param['label_type_sub'],
                       batch_size=param['batch_size'],
                       num_stack=param['num_stack'],
                       num_skip=param['num_skip'],
                       is_sorted=False)
    test_data = Dataset(data_type='test',
                        label_type_main='character',
                        label_type_sub='phone39',
                        batch_size=1,
                        num_stack=param['num_stack'],
                        num_skip=param['num_skip'],
                        is_sorted=False)

    # Tell TensorFlow that the model will be built into the default graph
    with tf.Graph().as_default():

        # Define placeholders
        network.inputs = tf.placeholder(
            tf.float32,
            shape=[None, None, network.input_size],
            name='input')
        indices_pl = tf.placeholder(tf.int64, name='indices')
        values_pl = tf.placeholder(tf.int32, name='values')
        shape_pl = tf.placeholder(tf.int64, name='shape')
        network.labels = tf.SparseTensor(indices_pl, values_pl, shape_pl)
        indices_sub_pl = tf.placeholder(tf.int64, name='indices_sub')
        values_sub_pl = tf.placeholder(tf.int32, name='values_sub')
        shape_sub_pl = tf.placeholder(tf.int64, name='shape_sub')
        network.labels_sub = tf.SparseTensor(indices_sub_pl,
                                             values_sub_pl,
                                             shape_sub_pl)
        network.inputs_seq_len = tf.placeholder(tf.int64,
                                                shape=[None],
                                                name='inputs_seq_len')
        network.keep_prob_input = tf.placeholder(tf.float32,
                                                 name='keep_prob_input')
        network.keep_prob_hidden = tf.placeholder(tf.float32,
                                                  name='keep_prob_hidden')

        # Add to the graph each operation
        loss_op, logits_main, logits_sub = network.compute_loss(
            network.inputs,
            network.labels,
            network.labels_sub,
            network.inputs_seq_len,
            network.keep_prob_input,
            network.keep_prob_hidden)
        train_op = network.train(
            loss_op,
            optimizer=param['optimizer'],
            learning_rate_init=float(param['learning_rate']),
            decay_steps=param['decay_steps'],
            decay_rate=param['decay_rate'])
        decode_op_main, decode_op_sub = network.decoder(
            logits_main,
            logits_sub,
            network.inputs_seq_len,
            decode_type='beam_search',
            beam_width=20)
        ler_op_main, ler_op_sub = network.compute_ler(
            decode_op_main, decode_op_sub,
            network.labels, network.labels_sub)

        # Build the summary tensor based on the TensorFlow collection of
        # summaries
        summary_train = tf.summary.merge(network.summaries_train)
        summary_dev = tf.summary.merge(network.summaries_dev)

        # Add the variable initializer operation
        init_op = tf.global_variables_initializer()

        # Create a saver for writing training checkpoints
        saver = tf.train.Saver(max_to_keep=None)

        # Count total parameters
        parameters_dict, total_parameters = count_total_parameters(
            tf.trainable_variables())
        for parameter_name in sorted(parameters_dict.keys()):
            print("%s %d" % (parameter_name, parameters_dict[parameter_name]))
        print("Total %d variables, %s M parameters" %
              (len(parameters_dict.keys()),
               "{:,}".format(total_parameters / 1000000)))

        csv_steps, csv_loss_train, csv_loss_dev = [], [], []
        csv_cer_train, csv_cer_dev = [], []
        csv_per_train, csv_per_dev = [], []
        # Create a session for running operation on the graph
        with tf.Session() as sess:

            # Instantiate a SummaryWriter to output summaries and the graph
            summary_writer = tf.summary.FileWriter(
                network.model_dir, sess.graph)

            # Initialize parameters
            sess.run(init_op)

            # Make mini-batch generator
            mini_batch_train = train_data.next_batch()
            mini_batch_dev = dev_data.next_batch()

            # Train model
            iter_per_epoch = int(train_data.data_num / param['batch_size'])
            train_step = train_data.data_num / param['batch_size']
            if train_step != int(train_step):
                iter_per_epoch += 1
            max_steps = iter_per_epoch * param['num_epoch']
            start_time_train = time.time()
            start_time_epoch = time.time()
            start_time_step = time.time()
            cer_dev_best = 1
            for step in range(max_steps):

                # Create feed dictionary for next mini batch (train)
                with tf.device('/cpu:0'):
                    inputs, labels_char, labels_phone, inputs_seq_len, _ = mini_batch_train.__next__()
                feed_dict_train = {
                    network.inputs: inputs,
                    network.labels: list2sparsetensor(labels_char,
                                                      padded_value=-1),
                    network.labels_sub: list2sparsetensor(labels_phone,
                                                          padded_value=-1),
                    network.inputs_seq_len: inputs_seq_len,
                    network.keep_prob_input: network.dropout_ratio_input,
                    network.keep_prob_hidden: network.dropout_ratio_hidden
                }

                # Update parameters
                sess.run(train_op, feed_dict=feed_dict_train)

                if (step + 1) % 10 == 0:

                    # Create feed dictionary for next mini batch (dev)
                    with tf.device('/cpu:0'):
                        inputs, labels_char, labels_phone, inputs_seq_len, _ = mini_batch_dev.__next__()
                    feed_dict_dev = {
                        network.inputs: inputs,
                        network.labels:  list2sparsetensor(labels_char,
                                                           padded_value=-1),
                        network.labels_sub: list2sparsetensor(labels_phone,
                                                              padded_value=-1),
                        network.inputs_seq_len: inputs_seq_len,
                        network.keep_prob_input: network.dropout_ratio_input,
                        network.keep_prob_hidden: network.dropout_ratio_hidden
                    }

                    # Compute loss
                    loss_train = sess.run(loss_op, feed_dict=feed_dict_train)
                    loss_dev = sess.run(loss_op, feed_dict=feed_dict_dev)
                    csv_steps.append(step)
                    csv_loss_train.append(loss_train)
                    csv_loss_dev.append(loss_dev)

                    # Change to evaluation mode
                    feed_dict_train[network.keep_prob_input] = 1.0
                    feed_dict_train[network.keep_prob_hidden] = 1.0
                    feed_dict_dev[network.keep_prob_input] = 1.0
                    feed_dict_dev[network.keep_prob_hidden] = 1.0

                    # Compute accuracy & update event file
                    cer_train, per_train, summary_str_train = sess.run(
                        [ler_op_main, ler_op_sub, summary_train],
                        feed_dict=feed_dict_train)
                    cer_dev, per_dev, summary_str_dev = sess.run(
                        [ler_op_main, ler_op_sub,  summary_dev],
                        feed_dict=feed_dict_dev)
                    csv_cer_train.append(cer_train)
                    csv_cer_dev.append(cer_dev)
                    csv_per_train.append(per_train)
                    csv_per_dev.append(per_dev)
                    summary_writer.add_summary(summary_str_train, step + 1)
                    summary_writer.add_summary(summary_str_dev, step + 1)
                    summary_writer.flush()

                    duration_step = time.time() - start_time_step
                    print("Step % d: loss = %.3f (%.3f) / cer = %.4f (%.4f) / per = % .4f (%.4f) (%.3f min)" %
                          (step + 1, loss_train, loss_dev, cer_train, cer_dev,
                           per_train, per_dev, duration_step / 60))
                    sys.stdout.flush()
                    start_time_step = time.time()

                # Save checkpoint and evaluate model per epoch
                if (step + 1) % iter_per_epoch == 0 or (step + 1) == max_steps:
                    duration_epoch = time.time() - start_time_epoch
                    epoch = (step + 1) // iter_per_epoch
                    print('-----EPOCH:%d (%.3f min)-----' %
                          (epoch, duration_epoch / 60))

                    # Save model (check point)
                    checkpoint_file = join(network.model_dir, 'model.ckpt')
                    save_path = saver.save(
                        sess, checkpoint_file, global_step=epoch)
                    print("Model saved in file: %s" % save_path)

                    if epoch >= 10:
                        start_time_eval = time.time()
                        print('=== Dev Data Evaluation ===')
                        cer_dev_epoch = do_eval_cer(
                            session=sess,
                            decode_op=decode_op_main,
                            network=network,
                            dataset=dev_data,
                            eval_batch_size=1,
                            is_multitask=True)
                        print('  CER: %f %%' % (cer_dev_epoch * 100))
                        per_dev_epoch = do_eval_per(
                            session=sess,
                            decode_op=decode_op_sub,
                            per_op=ler_op_sub,
                            network=network,
                            dataset=dev_data,
                            label_type=param['label_type_sub'],
                            eval_batch_size=1,
                            is_multitask=True)
                        print('  PER: %f %%' % (per_dev_epoch * 100))

                        if cer_dev_epoch < cer_dev_best:
                            cer_dev_best = cer_dev_epoch
                            print('■■■ ↑Best Score (CER)↑ ■■■')

                            print('=== Test Data Evaluation ===')
                            cer_test = do_eval_cer(
                                session=sess,
                                decode_op=decode_op_main,
                                network=network,
                                dataset=test_data,
                                eval_batch_size=1,
                                is_multitask=True)
                            print('  CER: %f %%' % (cer_test * 100))
                            per_test = do_eval_per(
                                session=sess,
                                decode_op=decode_op_sub,
                                per_op=ler_op_sub,
                                network=network,
                                dataset=test_data,
                                label_type=param['label_type_sub'],
                                eval_batch_size=1,
                                is_multitask=True)
                            print('  PER: %f %%' % (per_test * 100))

                        duration_eval = time.time() - start_time_eval
                        print('Evaluation time: %.3f min' %
                              (duration_eval / 60))

                        start_time_epoch = time.time()
                        start_time_step = time.time()

            duration_train = time.time() - start_time_train
            print('Total time: %.3f hour' % (duration_train / 3600))

            # Save train & dev loss, ler
            save_loss(csv_steps, csv_loss_train, csv_loss_dev,
                      save_path=network.model_dir)
            save_ler(csv_steps, csv_cer_train, csv_cer_dev,
                     save_path=network.model_dir)
            save_ler(csv_steps, csv_per_train, csv_per_dev,
                     save_path=network.model_dir)

            # Training was finished correctly
            with open(join(network.model_dir, 'complete.txt'), 'w') as f:
                f.write('')


def main(config_path):

    # Load a config file (.yml)
    with open(config_path, "r") as f:
        config = yaml.load(f)
        param = config['param']

    if param['label_type_sub'] == 'phone61':
        param['num_classes_sub'] = 61
    elif param['label_type_sub'] == 'phone48':
        param['num_classes_sub'] = 48
    elif param['label_type_sub'] == 'phone39':
        param['num_classes_sub'] = 39

    # Model setting
    CTCModel = load(model_type=param['model'])
    network = CTCModel(batch_size=param['batch_size'],
                       input_size=param['input_size'] * param['num_stack'],
                       num_unit=param['num_unit'],
                       num_layer_main=param['num_layer_main'],
                       num_layer_sub=param['num_layer_sub'],
                       num_classes_main=33,
                       num_classes_sub=param['num_classes_sub'],
                       main_task_weight=param['main_task_weight'],
                       parameter_init=param['weight_init'],
                       clip_grad=param['clip_grad'],
                       clip_activation=param['clip_activation'],
                       dropout_ratio_input=param['dropout_input'],
                       dropout_ratio_hidden=param['dropout_hidden'],
                       num_proj=param['num_proj'],
                       weight_decay=param['weight_decay'])

    network.model_name = param['model']
    network.model_name += '_' + str(param['num_unit'])
    network.model_name += '_main' + str(param['num_layer_main'])
    network.model_name += '_sub' + str(param['num_layer_sub'])
    network.model_name += '_' + param['optimizer']
    network.model_name += '_lr' + str(param['learning_rate'])
    if param['num_proj'] != 0:
        network.model_name += '_proj' + str(param['num_proj'])
    if param['dropout_input'] != 1:
        network.model_name += '_dropi' + str(param['dropout_input'])
    if param['dropout_hidden'] != 1:
        network.model_name += '_droph' + str(param['dropout_hidden'])
    if param['num_stack'] != 1:
        network.model_name += '_stack' + str(param['num_stack'])
    if param['weight_decay'] != 0:
        network.model_name += '_weightdecay' + str(param['weight_decay'])
    network.model_name += '_taskweight' + str(param['main_task_weight'])
    if param['decay_rate'] != 1:
        network.model_name += '_lrdecay' + \
            str(param['decay_steps'] + param['decay_rate'])

    # Set save path
    network.model_dir = mkdir('/n/sd8/inaguma/result/timit/')
    network.model_dir = mkdir_join(network.model_dir, 'ctc')
    network.model_dir = mkdir_join(
        network.model_dir, 'char_' + param['label_type_sub'])
    network.model_dir = mkdir_join(network.model_dir, network.model_name)

    # Reset model directory
    if not isfile(join(network.model_dir, 'complete.txt')):
        tf.gfile.DeleteRecursively(network.model_dir)
        tf.gfile.MakeDirs(network.model_dir)
    else:
        raise ValueError('File exists.')

    # Set process name
    setproctitle('timit_multictc')

    # Save config file
    shutil.copyfile(config_path, join(network.model_dir, 'config.yml'))

    sys.stdout = open(join(network.model_dir, 'train.log'), 'w')
    print(network.model_name)
    do_train(network=network, param=param)


if __name__ == '__main__':

    args = sys.argv
    if len(args) != 2:
        raise ValueError
    main(config_path=args[1])
