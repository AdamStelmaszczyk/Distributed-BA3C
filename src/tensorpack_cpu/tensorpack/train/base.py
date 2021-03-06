# -*- coding: utf-8 -*-
# File: base.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>

from abc import ABCMeta, abstractmethod
import signal
import re
from six.moves import range
import tqdm

import tensorflow as tf

from tensorpack.utils.utils import get_tqdm_kwargs
from .config import TrainConfig
from ..utils import *
from ..utils.timer import *
from ..utils.concurrency import start_proc_mask_signal
from ..callbacks import StatHolder
from ..tfutils import *
from ..tfutils.summary import create_summary

from tensorflow.python.framework import ops
from time import sleep
__all__ = ['Trainer']

import neptune_mp_server

from threading import Thread

class Trainer(object):
    """
    Base class for a trainer.

    Available Attritbutes:
        stat_holder: a `StatHolder` instance
        summary_writer: a `tf.SummaryWriter`
        config: a `TrainConfig`
        model: a `ModelDesc`
        global_step: a `int`
    """
    __metaclass__ = ABCMeta

    def __init__(self, config):
        """
        :param config: a `TrainConfig` instance
        """
        assert isinstance(config, TrainConfig), type(config)
        self.config = config
        self.model = config.model
        self.model.get_input_vars()  # ensure they are present
        self._extra_threads_procs = config.extra_threads_procs

    @abstractmethod
    def train(self):
        """ Start training"""
        pass

    @abstractmethod
    def run_step(self):
        """ run an iteration"""
        pass

    @abstractmethod
    def get_predict_func(self, input_names, output_names):
        """ return a online predictor"""
        pass

    def get_predict_funcs(self, input_names, output_names, n):
        """ return n predictor functions.
            Can be overwritten by subclasses to exploit more
            parallelism among funcs.
        """
        return [self.get_predict_func(input_names, output_names) for k in range(n)]

    def trigger_epoch(self):
        self._trigger_epoch()
        self.config.callbacks.trigger_epoch()
        self.summary_writer.flush()

    @abstractmethod
    def _trigger_epoch(self):
        """ This is called right after all steps in an epoch are finished"""
        pass

    def _init_summary(self):
        if not hasattr(logger, 'LOG_DIR'):
            raise RuntimeError("Please use logger.set_logger_dir at the beginning of your script.")
        self.summary_writer = tf.summary.FileWriter(
            logger.LOG_DIR, graph=tf.get_default_graph())
        self.summary_op = tf.summary.merge_all()
        # create an empty StatHolder
        self.stat_holder = StatHolder(logger.LOG_DIR)
        # save global_step in stat.json, but don't print it
        self.stat_holder.add_blacklist_tag(['global_step'])

    def _process_summary(self, summary_str):
        summary = tf.Summary.FromString(summary_str)
        for val in summary.value:
            if val.WhichOneof('value') == 'simple_value':
                val.tag = re.sub('tower[p0-9]+/', '', val.tag)   # TODO move to subclasses
                self.stat_holder.add_stat(val.tag, val.simple_value)
        self.summary_writer.add_summary(summary, self.global_step)

    def write_scalar_summary(self, name, val):
        self.summary_writer.add_summary(
                create_summary(name, val),
                get_global_step())
        self.stat_holder.add_stat(name, val)

    def main_loop(self):
        # some final operations that might modify the graph
        logger.info("[{}] Initializing graph variables ...".format(os.environ['SLURMD_NODENAME']))

        #self.sess.run(tf.initialize_all_variables())

        self.config.session_init.init(self.sess)
#        tf.get_default_graph().finalize()
        callbacks = self.config.callbacks
        logger.info("[{}] Starting concurrency...".format(os.environ['SLURMD_NODENAME']))
        self._start_concurrency()
        #with self.sess.as_default():
        logger.info("[{}] Setting default session".format(os.environ['SLURMD_NODENAME']))
        with ops.default_session(self.sess):
            try:
                logger.info("[{}] Getting global step".format(os.environ['SLURMD_NODENAME']))
                self.global_step = get_global_step()
                logger.info("[{}] Start training with global_step={}".format(os.environ['SLURMD_NODENAME'], self.global_step))

                if self.config.extra_arg['is_chief']:
                    server = neptune_mp_server.Server(
                            self.config.extra_arg['n_workers'],
                            port=self.config.extra_arg['port'],
                            debug_charts=self.config.extra_arg['debug_charts'],
                            adam_debug=self.config.extra_arg['adam_debug'],
                            schedule_hyper=self.config.extra_arg['schedule_hyper'],
                            experiment_dir=self.config.extra_arg['experiment_dir'])
                    server.main_loop()

                callbacks.before_train()
                for epoch in range(self.config.starting_epoch, self.config.max_epoch+1):
                    with timed_operation(
                        'Epoch {}, global_step={}'.format(
                            epoch, self.global_step + self.config.step_per_epoch)):
                        for step in tqdm.trange(
                                self.config.step_per_epoch,
                                **get_tqdm_kwargs(leave=True)):
                            if self.coord.should_stop():
                                return
                            self.run_step()
                            callbacks.trigger_step()
                            try:
                                self.global_step += 1
                            except:
                                self.global_step = -1
                        self.trigger_epoch()
                        print 'EPOCH ENDS HERE'
            except (KeyboardInterrupt, Exception):
                raise
            finally:
                # Do I need to run queue.close?
                print('Handling finally block')
                callbacks.after_train()
                self.coord.request_stop()
                self.summary_writer.close()
                self.sess.close()

    def init_session_and_coord(self):
        worker_host = self.config.extra_arg['worker_host']
        is_chief = self.config.extra_arg['is_chief']
        use_sync_opt = self.config.extra_arg['use_sync_opt']
        with tf.device('/cpu:0'):
            with tf.variable_scope(tf.get_variable_scope(), reuse=None):
                if False:
                    self.sess.set(tf.Session(target=worker_host,
                                           config=self.config.session_config))
                else:
                    logger.info("===============================================================")
                    if is_chief:
                        logger.info("CHIEF!")
                    logger.info("[{}] Creating the session".format(os.environ['SLURMD_NODENAME']))
                    logger.info("===============================================================")

                    if use_sync_opt == 0:
                        self.sess.set(tf.train.MonitoredTrainingSession(master=worker_host, is_chief=is_chief))
                    else: # use_sync_opt == 1
                        hooks = self.config.extra_arg['hooks']
                        self.sess.set(tf.train.MonitoredTrainingSession(master=worker_host, is_chief=is_chief, hooks=[hooks]))

                    logger.info("===============================================================")
                    logger.info("[{}] Session created".format(os.environ['SLURMD_NODENAME']))
                    logger.info("===============================================================")
                self.coord.set(tf.train.Coordinator())

    def _start_concurrency(self):
        """
        Run all threads before starting training
        """
        logger.info("Starting all threads & procs ...")
        tf.train.start_queue_runners(sess=self.sess.get(), coord=self.coord, daemon=True, start=True)

        #with self.sess.as_default():
        with ops.default_session(self.sess):
            # avoid sigint get handled by other processes
            start_proc_mask_signal(self._extra_threads_procs)

    def process_grads(self, grads):
        g = []
        for grad, var in grads:
            if grad is None:
                logger.warn("No Gradient w.r.t {}".format(var.op.name))
            else:
                g.append((grad, var))

        procs = self.config.model.get_gradient_processor()
        for proc in procs:
            g = proc.process(g)
        return g
