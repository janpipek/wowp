from __future__ import absolute_import, division, print_function, unicode_literals

import inspect
from collections import deque
import threading
import warnings
import wowp.components
import time
import datetime
from .logger import logger

try:
    from ipyparallel import Client, RemoteError
except ImportError:
    warnings.warn('ipyparallel not installed: IPyClusterScheduler cannot be used')


class _ActorRunner(object):
    """Base class for objects that run actors and process their results.

    It is ok, if the runner is a scheduler at the same time. The separation
    of concepts exists only for the cases when a scheduler needs to run
    actors in parallel (such as ThreadedScheduler).
    """

    def on_outport_put_value(self, outport):
        '''
        Propagates values put into an output port.

        Must be called after outport.put
        :param outport: output port
        :return: None
        '''
        if outport.connections:
            value = outport.pop()
            for inport in outport.connections:
                self.put_value(inport, value)

    def run_actor(self, actor):
        # print("Run actor")
        # TODO replace by an attribute / method call
        if isinstance(actor, wowp.components.Composite):
            self.run_workflow(actor)
        else:
            actor.scheduler = self
            args, kwargs = actor.get_run_args()
            result = actor.run(*args, **kwargs)
            # print("Result: ", result)
            if not result:
                return
            else:
                out_names = actor.outports.keys()
                if not hasattr(result, 'items'):
                    raise ValueError('The execute method must return '
                                     'a dict-like object with items method')
                for name, value in result.items():
                    if name in out_names:
                        outport = actor.outports[name]
                        outport.put(value)
                        self.on_outport_put_value(outport)
                    else:
                        raise ValueError("{} not in output ports".format(name))

    def run_workflow(self, workflow, **kwargs):
        inport_names = tuple(port.name for port in workflow.inports)
        if workflow.scheduler is not None:
            scheduler = workflow.scheduler
        else:
            scheduler = self
        for key, value in kwargs.items():
            if key not in inport_names:
                raise ValueError('{} is not an inport name'.format(key))
            inport = workflow.inports[key]
            # put values to connected ports
            scheduler.put_value(inport, kwargs[inport.name])
        # TODO can this be run inside self.execute itsef?
        scheduler.execute()

    def reset(self):
        """Reset the scheduler
        """
        # by default, this method does nothing
        pass

class NaiveScheduler(_ActorRunner):
    """Scheduler that directly calls connected actors.

    Problem: recursion quickly ends in full call stack.
    """

    def copy(self):
        return self

    def put_value(self, in_port, value):
        should_run = in_port.put(value)
        if should_run:
            self.run_actor(in_port.owner)

    def execute(self):
        pass


class LinearizedScheduler(_ActorRunner):
    """Scheduler that stacks all inputs in a queue and executes them in FIFO order."""

    def __init__(self):
        self.execution_queue = deque()

    def copy(self):
        return self.__class__()

    def put_value(self, in_port, value):
        self.execution_queue.appendleft((in_port, value))

    def execute(self):
        while self.execution_queue:
            in_port, value = self.execution_queue.pop()
            should_run = in_port.put(value)
            if should_run:
                self.run_actor(in_port.owner)


class _IPySystemJob(object):
    """
    System (local run) IPython parallel like job
    """

    def __init__(self, actor, *args, **kwargs):
        self.actor = actor
        self.args = args
        self.kwargs = kwargs
        self.started = datetime.datetime.now()
        self._res = self.actor.run(*self.args, **self.kwargs)
        self.completed = datetime.datetime.now()
        self.engine_id = None
        self.error = None

    def ready(self):
        return True

    def get(self):
        return self._res

    def display_outputs(self):
        pass


class IPyClusterScheduler(_ActorRunner):
    """
    Scheduler using ipyparallel cluster.

    Args:
        display_outputs (Optional[bool]): display stdout/err from actors [False]
        timeout (Optional): timeout in secs for waiting for ipyparallel cluster [60]
        min_engines (Optional[int]): minimum number of engines [1]
        *args: passed to self.init_cluster(*args, **kwargs)
        **kwargs: passed to self.init_cluster(*args, **kwargs)
    """

    def __init__(self, *args, **kwargs):
        self.process_pool = []
        # actor: job
        self._init_args = args
        self._init_kwargs = kwargs
        self.display_outputs = kwargs.pop('display_outputs', False)
        self.timeout = kwargs.pop('timeout', 60)
        self.min_engines = kwargs.pop('min_engines', 1)
        self.running_actors = {}
        self.execution_queue = deque()
        self.wait_queue = []
        self._ipy_rc = self.init_cluster(self.min_engines, self.timeout, *args, **kwargs)
        self._ipy_dv = self._ipy_rc[:]
        self._ipy_lv = self._ipy_rc.load_balanced_view()

    def copy(self):
        return self.__class__(*self._init_args, **self._init_kwargs)

    def put_value(self, in_port, value):
        self.execution_queue.appendleft((in_port, value))

    @staticmethod
    def init_cluster(min_engines, timeout, *args, **kwargs):
        '''Get a connection (view) to an IPython cluster

        Args:
            *args: passed to ipyparallel.Client(*args, **kwargs)
            **kwargs: passed to ipyparallel.Client(*args, **kwargs)
        '''

        maxtime = time.time() + timeout

        while True:
            try:
                cli = Client(*args, **kwargs)
            except Exception as e:
                if time.time() > maxtime:
                    # raise the original exception from ipyparallel
                    raise e
                else:
                    # sleep and try again to get the client
                    time.sleep(timeout * 0.1)
                    continue
            if len(cli.ids) >= min_engines:
                # we have enough clients
                break
            else:
                # free the client
                cli.close()
            if time.time() > maxtime:
                raise Exception('Not enough ipyparallel clients')
            # try ~10 times
            time.sleep(self.timeout * 0.1)

        return cli

    def execute(self):

        self.reset()

        while self.execution_queue or self.running_actors or self.wait_queue:
            self._try_empty_execution_queue()
            self._try_empty_wait_queue()
            self._try_empty_ready_jobs()
            # TODO shall we sleep here?
            # could also use ipyparallel callbacks

    def _try_empty_ready_jobs(self):
        pending = {}  # temporary container
        for actor, job_description in self.running_actors.items():

            job = job_description['job']

            if 'started' not in job_description:
                # log the started time
                if job_description['job'].started:
                    job_description['started'] = job_description['job'].started
                    job_description['engine'] = job_description['job'].engine_id
                    logger.debug('started {started} actor {actor}({args}, {kwargs})'
                                 ' on engine {engine}'.format(actor=actor.name,
                                                              **job_description))
            if job.ready():
                if 'started' not in job_description:
                    # log the started time
                    if job_description['job'].started:
                        job_description['started'] = job_description['job'].started
                        job_description['engine'] = job_description['job'].engine_id
                        logger.debug('started {started} actor {actor}({args}, {kwargs})'
                                     ' on engine {engine} at {started}'.format(
                                actor=actor.name,
                                **job_description))

                if 'completed' not in job_description:
                    # log the started time
                    if job_description['job'].started:
                        job_description['completed'] = job_description['job'].started
                        logger.debug('completed actor {actor} at {completed}'.format(
                                actor=actor.name,
                                **job_description))

                # process result
                # raise RemoteError in case of failure
                try:
                    result = job.get()
                except RemoteError:
                    logger.error('actor {} failed\n{}'.format(actor.name,
                                                              job_description['job'].error))
                    raise
                if self.display_outputs:
                    job.display_outputs()
                if result:
                    # empty results don't need any processing
                    out_names = actor.outports.keys()
                    if not hasattr(result, 'items'):
                        raise ValueError('The execute method must return '
                                         'a dict-like object with items method')
                    for name, value in result.items():
                        if name in out_names:
                            outport = actor.outports[name]
                            outport.put(value)
                            self.on_outport_put_value(outport)
                        else:
                            raise ValueError("{} not in output ports".format(name))

            else:
                pending[actor] = job_description
        # TODO temporary fix against spanning ipyparallel - NOT A GOOD WAY!
        time.sleep(0.1)
        self.running_actors = pending

    def _try_empty_wait_queue(self):
        pending = []  # temporary container
        for actor in self.wait_queue:
            # run actors only if not already running
            if actor not in self.running_actors:
                # TODO can we iterate and remove at the same time?
                self.running_actors[actor] = self.run_actor(actor)
            else:
                pending.append(actor)
        self.wait_queue = pending

    def _try_empty_execution_queue(self):
        while self.execution_queue:
            in_port, value = self.execution_queue.pop()
            should_run = in_port.put(value)
            if should_run:
                # waiting to be run
                self.wait_queue.append(in_port.owner)
                # self.running_actors((in_port.owner, self.run_actor(in_port.owner)))

    def run_actor(self, actor):
        # print("Run actor {}".format(actor))
        actor.scheduler = self
        args, kwargs = actor.get_run_args()
        # system actors must be run within this process
        res = dict(args=args, kwargs=kwargs)
        if actor.system_actor:
            res['job'] = _IPySystemJob(actor, *args, **kwargs)
        else:
            res['job'] = self._ipy_lv.apply_async(actor.run, *args, **kwargs)

        logger.debug('submitted actor {}({}, {})'.format(actor.name, args, kwargs))

        return res


class MultiIpyClusterScheduler(IPyClusterScheduler):
    """Scheduler using multiple ipyparallel clusters.

    Can use more than 1 ipyparallel cluster in order to avoid limitations for the number of engines.
    Either profiles or profile_dirs must be specified.

    Args:
        profiles (Optional[iterable]): list of ipyparallel profile names
        profile_dirs (Optional[iterable]): list of ipyparallel profile directories
        display_outputs (Optional[bool]): display stdout/err from actors [False]
        timeout (Optional): timeout in secs for waiting for ipyparallel cluster [60]
        min_engines (Optional[int]): minimum number of engines [1]
        client_kwargs: passed to ipyparallel.Client( **kwargs)
    """

    def __init__(self, profiles=(), profile_dirs=(), display_outputs=False, timeout=60, min_engines=1,
                 client_kwargs=None):
        self.process_pool = []
        # actor: job
        frame = inspect.currentframe()
        args, varargs, keywords, values = inspect.getargvalues(frame)
        self._init_args = args
        if keywords:
            self._init_kwargs = {k: values[k] for k in keywords}
        else:
            self._init_kwargs = {}
        self.display_outputs = display_outputs
        self.timeout = timeout
        self.min_engines = min_engines
        # get individual clients
        self._ipy_rc = []
        self._current_cli = 0
        self._ipy_lv = []
        self._ipy_dv = []
        # decide whether to use profiles or profile_dirs
        if profiles:
            cli_arg = 'profile'
            cli_args = profiles
        else:
            cli_arg = 'profile_dir'
            cli_args = profile_dirs
        if not cli_args:
            raise ValueError('Either profiles or profile_dirs must be specified')

        for value in cli_args:
            # init ipyparallel clients
            kwargs = {cli_arg: value}
            if client_kwargs:
                kwargs.update(client_kwargs)
            self._ipy_rc.append(self.init_cluster(min_engines, timeout, **kwargs))
            self._ipy_dv.append(self._ipy_rc[-1][:])
            self._ipy_lv.append(self._ipy_rc[-1].load_balanced_view())

        self.running_actors = {}
        self.execution_queue = deque()
        self.wait_queue = []

    def _rotate_client(self):
        current = self._current_cli
        self._current_cli += 1
        if self._current_cli == len(self._ipy_rc):
            self._current_cli = 0
        return current

    def run_actor(self, actor):
        # print("Run actor {}".format(actor))
        actor.scheduler = self
        args, kwargs = actor.get_run_args()
        # system actors must be run within this process
        res = dict(args=args, kwargs=kwargs)
        lv = self._ipy_lv[self._rotate_client()]
        if actor.system_actor:
            res['job'] = _IPySystemJob(actor, *args, **kwargs)
        else:
            res['job'] = lv.apply_async(actor.run, *args, **kwargs)

        logger.debug('submitted actor {}({}, {})'.format(actor.name, args, kwargs))

        return res


class ThreadedSchedulerWorker(threading.Thread, _ActorRunner):
    """Thread object that executes run after run of the ThreadedScheduler actors.
    """

    def __init__(self, scheduler, inner_id):
        threading.Thread.__init__(self)
        self.scheduler = scheduler
        self.executing = False
        self.finished = False
        self.inner_id = inner_id
        self.state_mutex = threading.RLock()

    def run(self):
        from time import sleep
        while not self.finished:
            pv = self.scheduler.pop_idle_task()
            if pv:
                port, value = pv
                should_run = port.put(value)  # Change to if
                if should_run:
                    # print(self.inner_id, port.owner.name, value)
                    self.run_actor(port.owner)
                else:
                    pass
                    # print(self.inner_id, "Won't run", )
                self.scheduler.on_actor_finished(port.owner)
            else:
                pass
                # print(self.inner_id, "Nothing to do")
                sleep(0.02)
                # print(self.inner_id, "End")

    def put_value(self, in_port, value):
        # print(self.inner_id, " worker put ", value)
        self.scheduler.put_value(in_port, value)

    def finish(self):
        """Finish after the current running job is done."""
        with self.state_mutex:
            self.finished = True


class ThreadedScheduler(object):
    def __init__(self, max_threads=2):
        self.max_threads = max_threads
        self.threads = []
        self.execution_queue = deque()
        self.running_actors = []
        self.state_mutex = threading.RLock()

    def copy(self):
        return self.__class__(max_threads=self.max_threads)

    def pop_idle_task(self):
        with self.state_mutex:
            for port, value in self.execution_queue:
                if port.owner not in self.running_actors:
                    # Removes first occurrence - it's probably safe
                    # print("Removing", value)
                    self.execution_queue.remove((port, value))
                    self.running_actors.append(port.owner)
                    return port, value
            else:
                return None

    def put_value(self, in_port, value):
        with self.state_mutex:  # Probably not necessary
            # print("put ", value, ", in queue: ", len(self.queue))
            self.execution_queue.append((in_port, value))

    def is_running(self):
        with self.state_mutex:
            return bool(self.running_actors or self.execution_queue)

    def on_actor_finished(self, actor):
        with self.state_mutex:
            self.running_actors.remove(actor)
            if not self.is_running():
                self.finish_all_threads()

    def execute(self):
        with self.state_mutex:  # Probably not necessary
            for i in range(self.max_threads):
                thread = ThreadedSchedulerWorker(self, i)
                self.threads.append(thread)
                thread.start()
                # print("Thread started", thread.ident)
        for thread in self.threads:
            # print("Join thread")
            thread.join()

    def finish_all_threads(self):
        # print("Everything finished. Waiting for threads to end.")
        for thread in self.threads:
            thread.finish()
