from __future__ import print_function, division, absolute_import

from collections import defaultdict, deque
from datetime import datetime
from functools import partial
import logging
from math import ceil
import socket
from time import time
import uuid

from toolz import frequencies, memoize, concat, identity, valmap
from tornado import gen
from tornado.gen import Return
from tornado.queues import Queue
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.iostream import StreamClosedError, IOStream

from dask.core import istask, get_deps, reverse_dict
from dask.order import order

from .core import (rpc, coerce_to_rpc, connect, read, write, MAX_BUFFER_SIZE,
        Server, send_recv)
from .client import unpack_remotedata, scatter_to_workers, gather_from_workers
from .utils import (All, ignoring, clear_queue, _deps, get_ip,
        ignore_exceptions, ensure_ip)


logger = logging.getLogger(__name__)


class Scheduler(Server):
    """ Dynamic distributed task scheduler

    The scheduler tracks the current state of workers, data, and computations.
    The scheduler listens for events and responds by controlling workers
    appropriately.  It continuously tries to use the workers to execute an ever
    growing dask graph.

    All events are handled quickly, in linear time with respect to their input
    (which is often of constant size) and generally within a millisecond.  To
    accomplish this the scheduler tracks a lot of state.  Every operation
    maintains the consistency of this state.

    The scheduler communicates with the outside world either by adding pairs of
    in/out queues or by responding to a new IOStream (the Scheduler can operate
    as a typical distributed ``Server``).  It maintains a consistent and valid
    view of the world even when listening to several clients at once.

    A Scheduler is typically started either with the ``dscheduler``
    executable::

        $ dscheduler 127.0.0.1:8787  # address of center

    Or as part of when an Executor starts up and connects to a Center::

        >>> e = Executor('127.0.0.1:8787')  # doctest: +SKIP
        >>> e.scheduler  # doctest: +SKIP
        Scheduler(...)

    Users typically do not interact with the scheduler except through Plugins.
    See http://distributed.readthedocs.org/en/latest/plugins.html

    **State**

    * **dask:** ``{key: task}``:
        Dask graph of all computations to perform
    * **dependencies:** ``{key: {key}}``:
        Dictionary showing which keys depend on which others
    * **dependents:** ``{key: {key}}``:
        Dictionary showing which keys are dependent on  which others
    * **waiting:** ``{key: {key}}``:
        Dictionary like dependencies but excludes keys already computed
    * **waiting_data:** ``{key: {key}}``:
        Dictionary like dependents but excludes keys already computed
    * **ncores:** ``{worker: int}``:
        Number of cores owned by each worker
    * **nannies:** ``{worker: port}``:
        Port of nanny process for each worker
    * **who_has:** ``{key: {worker}}``:
        Where each key lives.  The current state of distributed memory.
    * **has_what:** ``{worker: {key}}``:
        What worker has what keys.  The transpose of who_has.
    * **processing:** ``{worker: {keys}}``:
        Set of keys currently in execution on each worker
    * **stacks:** ``{worker: [keys]}``:
        List of keys waiting to be sent to each worker
    * **retrictions:** ``{key: {hostnames}}``:
        A set of hostnames per key of where that key can be run.  Usually this
        is empty unless a key has been specifically restricted to only run on
        certain hosts.  These restrictions don't include a worker port.  Any
        worker on that hostname is deemed valid.
    * **loose_retrictions:** ``{key}``:
        Set of keys for which we are allow to violate restrictions (see above)
        if not valid workers are present.
    * **held_data:** ``{key}``:
        A set of keys that we are not allowed to garbage collect
    * **in_play:** ``{key}``:
        All keys in one of who_has, waiting, stacks, processing.  This is any
        key that will eventually be in memory.
    * **keyorder:** ``{key: tuple}``:
        A score per key that determines its priority
    * **scheduler_queues:** ``[Queues]``:
        A list of Tornado Queues from which we accept stimuli
    * **report_queues:** ``[Queues]``:
        A list of Tornado Queues on which we report results
    * **streams:** ``[IOStreams]``:
        A list of Tornado IOStreams from which we both accept stimuli and
        report results
    * **coroutines:** ``[Futures]``:
        A list of active futures that control operation
    *  **exceptions:** ``{key: Exception}``:
        A dict mapping keys to remote exceptions
    *  **tracebacks:** ``{key: list}``:
        A dict mapping keys to remote tracebacks stored as a list of strings
    *  **exceptions_blame:** ``{key: key}``:
        A dict mapping a key to another key on which it depends that has failed
    *  **resource_logs:** ``list``:
        A list of dicts from the nannies, tracking resources on the workers
    *  **deleted_keys:** ``{key: {workers}}``
        Locations of workers that have keys that should be deleted
    *  **loop:** ``IOLoop``:
        The running Torando IOLoop
    """
    def __init__(self, center=None, loop=None,
            resource_interval=1, resource_log_size=1000,
            max_buffer_size=MAX_BUFFER_SIZE, delete_interval=500,
            ip=None, **kwargs):
        self.scheduler_queues = [Queue()]
        self.report_queues = []
        self.streams = []
        self.status = None
        self.coroutines = []
        self.ip = ip or get_ip()
        self.delete_interval = delete_interval

        if center:
            self.center = coerce_to_rpc(center)
        else:
            self.center = None

        self.dask = dict()
        self.dependencies = dict()
        self.dependents = dict()
        self.generation = 0
        self.has_what = defaultdict(set)
        self.held_data = set()
        self.in_play = set()
        self.keyorder = dict()
        self.nbytes = dict()
        self.ncores = dict()
        self.nannies = dict()
        self.processing = dict()
        self.restrictions = dict()
        self.loose_restrictions = set()
        self.stacks = dict()
        self.waiting = dict()
        self.waiting_data = dict()
        self.who_has = defaultdict(set)
        self.deleted_keys = defaultdict(set)

        self.exceptions = dict()
        self.tracebacks = dict()
        self.exceptions_blame = dict()
        self.resource_logs = dict()

        self.loop = loop or IOLoop.current()
        self.io_loop = self.loop

        self.resource_interval = resource_interval
        self.resource_log_size = resource_log_size

        self._rpcs = dict()

        self.plugins = []

        self.compute_handlers = {'update-graph': self.update_graph,
                                 'update-data': self.update_data,
                                 'missing-data': self.mark_missing_data,
                                 'release-held-data': self.release_held_data,
                                 'restart': self.restart}

        self.handlers = {'start-control': self.control_stream,
                         'scatter': self.scatter,
                         'register': self.add_worker,
                         'unregister': self.remove_worker,
                         'gather': self.gather,
                         'feed': self.feed,
                         'terminate': self.close,
                         'broadcast': self.broadcast,
                         'ncores': self.get_ncores,
                         'has_what': self.get_has_what,
                         'who_has': self.get_who_has}

        super(Scheduler, self).__init__(handlers=self.handlers,
                max_buffer_size=max_buffer_size, **kwargs)

    def rpc(self, ip, port):
        """ Cached rpc objects """
        if (ip, port) not in self._rpcs:
            self._rpcs[(ip, port)] = rpc(ip=ip, port=port)
        return self._rpcs[(ip, port)]

    @property
    def address(self):
        return (self.ip, self.port)

    def identity(self, stream):
        """ Basic information about ourselves and our cluster """
        d = {'type': type(self).__name__, 'id': self.id,
             'workers': list(self.ncores)}
        if self.center:
            d['center'] = (self.center.ip, self.center.port)
        return d

    def put(self, msg):
        """ Place a message into the scheduler's queue """
        return self.scheduler_queues[0].put_nowait(msg)

    @gen.coroutine
    def sync_center(self):
        """ Connect to center, determine available workers """
        self.ncores, self.has_what, self.who_has, self.nannies = yield [
                self.center.ncores(),
                self.center.has_what(),
                self.center.who_has(),
                self.center.nannies()]

        self._nanny_coroutines = []
        for (ip, wport), nport in self.nannies.items():
            if not nport:
                continue
            if (ip, nport) not in self.resource_logs:
                self.resource_logs[(ip, nport)] = deque(maxlen=self.resource_log_size)

            self._nanny_coroutines.append(self.nanny_listen(ip, nport))

    def start(self, start_queues=True):
        """ Clear out old state and restart all running coroutines """
        collections = [self.dask, self.dependencies, self.dependents,
                self.waiting, self.waiting_data, self.in_play, self.keyorder,
                self.nbytes, self.processing, self.restrictions,
                self.loose_restrictions]
        for collection in collections:
            collection.clear()

        self.processing = {addr: set() for addr in self.ncores}
        self.stacks = {addr: list() for addr in self.ncores}

        self.worker_queues = {addr: Queue() for addr in self.ncores}

        with ignoring(AttributeError):
            for c in self._worker_coroutines:
                c.cancel()

        self._worker_coroutines = [self.worker(w) for w in self.ncores]
        self._delete_periodic_callback = \
                PeriodicCallback(callback=self.clear_data_from_workers,
                                 callback_time=self.delete_interval,
                                 io_loop=self.loop)
        self._delete_periodic_callback.start()

        self.heal_state()

        self.status = 'running'

        if start_queues:
            self.handle_queues(self.scheduler_queues[0], None)

        for cor in self.coroutines:
            if cor.done():
                raise cor.exception()

        return self.finished()

    @gen.coroutine
    def finished(self):
        """ Wait until all coroutines have ceased """
        while any(not c.done() for c in self.coroutines):
            yield All(self.coroutines)

    @gen.coroutine
    def close(self, stream=None):
        """ Send cleanup signal to all coroutines then wait until finished

        See Also
        --------
        Scheduler.cleanup
        """
        yield self.cleanup()
        yield self.finished()
        if self.center:
            yield self.center.close(close=True)
            self.center.close_streams()
        self.status = 'closed'

    @gen.coroutine
    def cleanup(self):
        """ Clean up queues and coroutines, prepare to stop """
        if self.status == 'closing':
            raise gen.Return()

        self.status = 'closing'
        logger.debug("Cleaning up coroutines")
        n = 0
        for w, nc in self.ncores.items():
            for i in range(nc):
                self.worker_queues[w].put_nowait({'op': 'close'}); n += 1

        for s in self.scheduler_queues[1:]:
            s.put_nowait({'op': 'close-stream'})

        for i in range(n):
            msg = yield self.scheduler_queues[0].get()

        for q in self.report_queues:
            q.put_nowait({'op': 'close'})

    def mark_ready_to_run(self, key):
        """ Send task to an appropriate worker.  Trigger that worker.

        See Also
        --------
        decide_worker
        Scheduler.ensure_occupied
        """
        logger.debug("Mark %s ready to run", key)
        if key in self.waiting:
            assert not self.waiting[key]
            del self.waiting[key]

        new_worker = decide_worker(self.dependencies, self.stacks,
                self.who_has, self.restrictions, self.loose_restrictions,
                self.nbytes, key)

        self.stacks[new_worker].append(key)
        self.ensure_occupied(new_worker)

    def mark_key_in_memory(self, key, workers=None):
        """ Mark that a key now lives in distributed memory """
        logger.debug("Mark %s in memory", key)
        if workers is None:
            workers = self.who_has[key]
        for worker in workers:
            self.who_has[key].add(worker)
            self.has_what[worker].add(key)
            with ignoring(KeyError):
                self.processing[worker].remove(key)

        for dep in sorted(self.dependents.get(key, []), key=self.keyorder.get,
                          reverse=True):
            if dep in self.waiting:
                s = self.waiting[dep]
                with ignoring(KeyError):
                    s.remove(key)
                if not s:  # new task ready to run
                    self.mark_ready_to_run(dep)

        for dep in self.dependencies.get(key, []):
            if dep in self.waiting_data:
                s = self.waiting_data[dep]
                with ignoring(KeyError):
                    s.remove(key)
                if not s and dep and dep not in self.held_data:
                    self.delete_data(keys=[dep])

        self.report({'op': 'key-in-memory',
                     'key': key,
                     'workers': workers})

    def ensure_occupied(self, worker):
        """ Send tasks to worker while it has tasks and free cores """
        logger.debug('Ensure worker is occupied: %s', worker)
        while (self.stacks[worker] and
               self.ncores[worker] > len(self.processing[worker])):
            key = self.stacks[worker].pop()
            self.processing[worker].add(key)
            logger.debug("Send job to worker: %s, %s, %s", worker, key, self.dask[key])
            self.worker_queues[worker].put_nowait(
                    {'op': 'compute-task',
                     'key': key,
                     'task': self.dask[key],
                     'who_has': {dep: self.who_has[dep] for dep in
                                 self.dependencies[key]}})

    def seed_ready_tasks(self, keys=None):
        """ Distribute many leaf tasks among workers

        Takes an iterable of keys to consider for execution

        See Also
        --------
        assign_many_tasks
        Scheduler.ensure_occupied
        """
        if keys is None:
            keys = self.dask
        new_stacks = assign_many_tasks(
                self.dependencies, self.waiting, self.keyorder, self.who_has,
                self.stacks, self.restrictions, self.loose_restrictions,
                self.nbytes,
                [k for k in keys if k in self.waiting and not self.waiting[k]])
        logger.debug("Seed ready tasks: %s", new_stacks)
        for worker, stack in new_stacks.items():
            if stack:
                self.ensure_occupied(worker)

    def update_data(self, who_has=None, nbytes=None):
        """
        Learn that new data has entered the network from an external source

        See Also
        --------
        Scheduler.mark_key_in_memory
        """
        logger.debug("Update data %s", who_has)
        for key, workers in who_has.items():
            self.mark_key_in_memory(key, workers)

        self.nbytes.update(nbytes)

        self.held_data.update(who_has)
        self.in_play.update(who_has)

    def mark_task_erred(self, key, worker, exception, traceback):
        """ Mark that a task has erred on a particular worker

        See Also
        --------
        Scheduler.mark_failed
        """
        if key in self.processing[worker]:
            self.processing[worker].remove(key)
            self.exceptions[key] = exception
            self.tracebacks[key] = traceback
            self.mark_failed(key, key)
            self.ensure_occupied(worker)
            for plugin in self.plugins[:]:
                try:
                    plugin.task_erred(self, key, worker, exception)
                except Exception as e:
                    logger.exception(e)

    def mark_failed(self, key, failing_key=None):
        """ When a task fails mark it and all dependent task as failed """
        logger.debug("Mark key as failed %s", key)
        if key in self.exceptions_blame:
            return
        self.exceptions_blame[key] = failing_key
        self.report({'op': 'task-erred',
                     'key': key,
                     'exception': self.exceptions[failing_key],
                     'traceback': self.tracebacks[failing_key]})
        if key in self.waiting:
            del self.waiting[key]
        if key in self.waiting_data:
            del self.waiting_data[key]
        self.in_play.remove(key)
        for dep in self.dependents[key]:
            self.mark_failed(dep, failing_key)

    def mark_task_finished(self, key, worker, nbytes):
        """ Mark that a task has finished execution on a particular worker """
        logger.debug("Mark task as finished %s, %s", key, worker)
        if key in self.processing[worker]:
            self.nbytes[key] = nbytes
            self.mark_key_in_memory(key, [worker])
            self.ensure_occupied(worker)
            for plugin in self.plugins[:]:
                try:
                    plugin.task_finished(self, key, worker, nbytes)
                except Exception as e:
                    logger.exception(e)
        else:
            logger.debug("Key not found in processing, %s, %s, %s",
                         key, worker, self.processing[worker])

    def mark_missing_data(self, missing=None, key=None, worker=None):
        """ Mark that certain keys have gone missing.  Recover.

        See Also
        --------
        heal_missing_data
        """
        missing = set(missing)
        logger.debug("Recovering missing data: %s", missing)
        for k in missing:
            with ignoring(KeyError):
                workers = self.who_has.pop(k)
                for worker in workers:
                    self.has_what[worker].remove(k)
        self.my_heal_missing_data(missing)

        if key and worker:
            with ignoring(KeyError):
                self.processing[worker].remove(key)
            self.waiting[key] = missing
            logger.debug('task missing data, %s, %s', key, self.waiting)
            self.ensure_occupied(worker)

        self.seed_ready_tasks()

    def log_state(self, msg=''):
        """ Log current full state of the scheduler """
        logger.debug("Runtime State: %s", msg)
        logger.debug('\n\nwaiting: %s\n\nstacks: %s\n\nprocessing: %s\n\n'
                'in_play: %s\n\n', self.waiting, self.stacks, self.processing,
                self.in_play)

    def remove_worker(self, stream=None, address=None, heal=True):
        """ Mark that a worker no longer seems responsive

        See Also
        --------
        Scheduler.heal_state
        """
        logger.debug("Remove worker %s", address)
        if address not in self.processing:
            return
        keys = self.has_what.pop(address)
        for i in range(self.ncores[address]):  # send close message, in case not dead
            self.worker_queues[address].put_nowait({'op': 'close', 'report': False})
        del self.worker_queues[address]
        del self.ncores[address]
        del self.stacks[address]
        del self.processing[address]
        del self.nannies[address]
        if not self.stacks:
            logger.critical("Lost all workers")
        missing_keys = set()
        for key in keys:
            self.who_has[key].remove(address)
            if not self.who_has[key]:
                missing_keys.add(key)
        gone_data = {k for k, v in self.who_has.items() if not v}
        self.in_play.difference_update(missing_keys)
        for k in gone_data:
            del self.who_has[k]

        if heal:
            self.heal_state()

    def add_worker(self, stream=None, address=None, keys=(), ncores=None,
                   nanny_port=None):
        self.ncores[address] = ncores
        self.nannies[address] = nanny_port
        if address not in self.processing:
            self.has_what[address] = set()
            self.processing[address] = set()
            self.stacks[address] = []
            self.worker_queues[address] = Queue()
        for key in keys:
            self.mark_key_in_memory(key, [address])

        self._worker_coroutines.append(self.worker(address))

        logger.info("Register %s", str(address))
        return b'OK'

    def update_graph(self, dsk=None, keys=None, restrictions=None,
                     loose_restrictions=None):
        """ Add new computations to the internal dask graph

        This happens whenever the Executor calls submit, map, get, or compute.
        """
        for k in list(dsk):
            if dsk[k] is k:
                del dsk[k]

        update_state(self.dask, self.dependencies, self.dependents,
                self.held_data, self.who_has, self.in_play,
                self.waiting, self.waiting_data, dsk, keys)

        cover_aliases(self.dask, dsk)

        if restrictions:
            restrictions = {k: set(map(ensure_ip, v))
                            for k, v in restrictions.items()}
            self.restrictions.update(restrictions)
        if loose_restrictions:
            self.loose_restrictions |= loose_restrictions

        new_keyorder = order(dsk)  # TODO: define order wrt old graph
        for key in new_keyorder:
            if key not in self.keyorder:
                # TODO: add test for this
                self.keyorder[key] = (self.generation, new_keyorder[key]) # prefer old
        if len(dsk) > 1:
            self.generation += 1  # older graph generations take precedence

        for key in dsk:
            for dep in self.dependencies[key]:
                if dep in self.exceptions_blame:
                    self.mark_failed(key, self.exceptions_blame[dep])

        self.seed_ready_tasks(dsk)
        for key in keys:
            if self.who_has[key]:
                self.mark_key_in_memory(key)

        for plugin in self.plugins[:]:
            try:
                plugin.update_graph(self, dsk, keys, restrictions or {})
            except Exception as e:
                logger.exception(e)

    def release_held_data(self, keys=None):
        """ Mark that a key is no longer externally required to be in memory """
        keys = set(keys)
        keys &= self.held_data
        if keys:
            logger.debug("Release keys: %s", keys)
            self.held_data -= keys
            keys2 = {k for k in keys if not self.waiting_data.get(k)}
            if keys2:
                self.delete_data(keys=keys2)  # async

    def heal_state(self):
        """ Recover from catastrophic change """
        logger.debug("Heal state")
        self.log_state("Before Heal")
        state = heal(self.dependencies, self.dependents, set(self.who_has),
                self.stacks, self.processing, self.waiting, self.waiting_data)
        released = state['released']
        self.in_play.clear(); self.in_play.update(state['in_play'])
        add_keys = {k for k, v in self.waiting.items() if not v}
        for key in self.held_data & released:
            self.report({'op': 'lost-key', 'key': key})
        if self.stacks:
            for key in add_keys:
                self.mark_ready_to_run(key)

        self.delete_data(keys=set(self.who_has) & released - self.held_data)
        self.in_play.update(self.who_has)
        self.log_state("After Heal")

    def my_heal_missing_data(self, missing):
        """ Recover from lost data """
        logger.debug("Heal from missing data")
        return heal_missing_data(self.dask, self.dependencies, self.dependents,
                self.held_data, self.who_has, self.in_play, self.waiting,
                self.waiting_data, missing)

    def report(self, msg):
        """ Publish updates to all listening Queues and Streams """
        for q in self.report_queues:
            q.put_nowait(msg)
        for s in self.streams:
            try:
                write(s, msg)  # asynchrnous
            except StreamClosedError:
                logger.critical("Tried writing to closed stream: %s", msg)

    def add_plugin(self, plugin):
        """ Add external plugin to scheduler

        See http://http://distributed.readthedocs.org/en/latest/plugins.html
        """
        self.plugins.append(plugin)

    def handle_queues(self, scheduler_queue, report_queue):
        """ Register new control and report queues to the Scheduler """
        self.scheduler_queues.append(scheduler_queue)
        if report_queue:
            self.report_queues.append(report_queue)
        future = self.handle_messages(scheduler_queue, report_queue)
        self.coroutines.append(future)
        return future

    @gen.coroutine
    def control_stream(self, stream, address=None):
        """ Listen to messages from an IOStream """
        ident = str(uuid.uuid1())
        logger.info("Connection to %s, %s", type(self).__name__, ident)
        self.streams.append(stream)
        try:
            yield self.handle_messages(stream, stream)
        finally:
            if not stream.closed():
                yield write(stream, {'op': 'stream-closed'})
                stream.close()
            self.streams.remove(stream)
            logger.info("Close connection to %s, %s", type(self).__name__,
                    ident)

    @gen.coroutine
    def handle_messages(self, in_queue, report):
        """ Master coroutine.  Handles inbound messages.

        This runs once per Queue or Stream.
        """
        if isinstance(in_queue, Queue):
            next_message = in_queue.get
        elif isinstance(in_queue, IOStream):
            next_message = lambda: read(in_queue)
        else:
            raise NotImplementedError()

        if isinstance(report, Queue):
            put = report.put_nowait
        elif isinstance(report, IOStream):
            put = lambda msg: write(report, msg)
        else:
            put = lambda msg: None
        put({'op': 'stream-start'})

        while True:
            msg = yield next_message()  # in_queue.get()
            logger.debug("scheduler receives message %s", msg)
            op = msg.pop('op')

            if op == 'close-stream':
                break
            elif op == 'close':
               self.close()
               break
            elif op in self.compute_handlers:
                try:
                    result = self.compute_handlers[op](**msg)
                    if isinstance(result, gen.Future):
                        yield result
                except Exception as e:
                    logger.exception(e)
                    raise
            else:
                logger.warn("Bad message: op=%s, %s", op, msg)

            if op == 'close':
                break

        logger.debug('Finished scheduling coroutine')

    @gen.coroutine
    def worker(self, ident):
        """ Manage a single distributed worker node

        This coroutine manages one remote worker.  It spins up several
        ``worker_core`` coroutines, one for each core.  It reports a closed
        connection to scheduler if one occurs.
        """
        try:
            yield All([self.worker_core(ident, i)
                    for i in range(self.ncores[ident])])
        except (IOError, OSError):
            logger.info("Worker failed from closed stream: %s", ident)
            self.remove_worker(address=ident)

    @gen.coroutine
    def worker_core(self, ident, i):
        """ Manage one core on one distributed worker node

        This coroutine listens on worker_queue for the following operations

        **Incoming Messages**:

        - compute-task:  call worker.compute(...) on remote node, report when done
        - close: close connection to worker node, report `worker-finished` to
          scheduler

        See Also
        --------
        Scheduler.mark_task_finished
        Scheduler.mark_task_erred
        Scheduler.mark_missing_data
        distributed.worker.Worker.compute
        """
        worker = rpc(ip=ident[0], port=ident[1])
        logger.debug("Start worker core %s, %d", ident, i)

        while True:
            msg = yield self.worker_queues[ident].get()
            if msg['op'] == 'close':
                logger.debug("Worker core receives close message %s, %s",
                        ident, msg)
                break
            if msg['op'] == 'compute-task':
                key = msg['key']
                who_has = msg['who_has']
                task = msg['task']
                if not istask(task):
                    response, content = yield worker.update_data(
                            data={key: task}, report=self.center is not None)
                    assert response == b'OK', response
                    nbytes = content['nbytes'][key]
                else:
                    response, content = yield worker.compute(function=execute_task,
                                                             args=(task,),
                                                             who_has=who_has,
                                                             key=key,
                                                             kwargs={},
                                                             report=self.center
                                                                     is not None)
                    if response == b'OK':
                        nbytes = content['nbytes']
                logger.debug("Compute response from worker %s, %s, %s, %s",
                             ident, key, response, content)
                if response == b'error':
                    error, traceback = content
                    self.mark_task_erred(key, ident, error, traceback)

                elif response == b'missing-data':
                    self.mark_missing_data(content.args, key=key, worker=ident)

                else:
                    self.mark_task_finished(key, ident, nbytes)

        yield worker.close(close=True)
        worker.close_streams()
        if msg.get('report', True):
            self.put({'op': 'worker-finished',
                      'worker': ident})
        logger.debug("Close worker core, %s, %d", ident, i)

    @gen.coroutine
    def clear_data_from_workers(self):
        """ This is intended to be run periodically,

        The ``self._delete_periodic_callback`` attribute holds a PeriodicCallback
        that runs this every ``self.delete_interval`` milliseconds``.
        """
        if self.deleted_keys:
            d = self.deleted_keys.copy()
            self.deleted_keys.clear()

            coroutines = [self.rpc(ip=worker[0], port=worker[1]).delete_data(
                                    keys=keys, report=False)
                          for worker, keys in d.items()]
            for worker, keys in d.items():
                logger.debug("Remove %d keys from worker %s", len(keys), worker)
            yield ignore_exceptions(coroutines, socket.error, StreamClosedError)

        raise Return(b'OK')

    def delete_data(self, stream=None, keys=None):
        who_has2 = {k: v for k, v in self.who_has.items() if k in keys}

        for key in keys:
            for worker in self.who_has[key]:
                self.has_what[worker].remove(key)
                self.deleted_keys[worker].add(key)
            del self.who_has[key]
            if key in self.waiting_data:
                del self.waiting_data[key]
            if key in self.in_play:
                self.in_play.remove(key)

    @gen.coroutine
    def nanny_listen(self, ip, port):
        """ Listen to a nanny for monitoring information """
        stream = yield connect(ip=ip, port=port)
        yield write(stream, {'op': 'monitor_resources',
                             'interval': self.resource_interval})
        while not stream.closed():
            msg = yield read(stream)
            if not msg:
                stream.close()
            self.resource_logs[(ip, port)].append(msg)

    @gen.coroutine
    def scatter(self, stream=None, data=None, workers=None):
        """ Send data out to workers """
        if not self.ncores:
            raise ValueError("No workers yet found.  "
                             "Try syncing with center.\n"
                             "  e.sync_center()")
        ncores = workers if workers is not None else self.ncores
        remotes, who_has, nbytes = yield scatter_to_workers(
                                            self.center or self.address,
                                            ncores, data,
                                            report=not not self.center)
        self.update_data(who_has=who_has, nbytes=nbytes)

        raise gen.Return(remotes)

    @gen.coroutine
    def gather(self, stream=None, keys=None):
        """ Collect data in from workers """
        keys = list(keys)
        who_has = {key: self.who_has[key] for key in keys}

        try:
            data = yield gather_from_workers(who_has)
            result = (b'OK', data)
        except KeyError as e:
            logger.debug("Couldn't gather keys %s", e)
            result = (b'error', e)

        raise gen.Return(result)

    @gen.coroutine
    def restart(self):
        """ Restart all workers.  Reset local state """
        logger.debug("Send shutdown signal to workers")

        for q in self.scheduler_queues + self.report_queues:
            clear_queue(q)

        nannies = self.nannies.copy()

        for addr in nannies:
            self.remove_worker(address=addr, heal=False)

        logger.debug("Send kill signal to nannies")
        nannies = [rpc(ip=ip, port=n_port)
                   for (ip, w_port), n_port in nannies.items()]
        yield All([nanny.kill() for nanny in nannies])

        while self.ncores:
            yield gen.sleep(0.01)


        # All quiet
        resps = yield All([nanny.instantiate(close=True) for nanny in nannies])
        assert all(resp == b'OK' for resp in resps)
        if self.center:
            yield self.sync_center()
        self.start()

        # self.who_has.clear()
        # self.has_what.clear()

        self.report({'op': 'restart'})
        for plugin in self.plugins[:]:
            try:
                plugin.restart(self)
            except Exception as e:
                logger.exception(e)

    def validate(self, allow_overlap=False):
        in_memory = {k for k, v in self.who_has.items() if v}
        validate_state(self.dependencies, self.dependents, self.waiting,
                self.waiting_data, in_memory, self.stacks,
                self.processing, None, set(), self.in_play,
                allow_overlap=allow_overlap)
        assert (set(self.ncores) == \
                set(self.has_what) == \
                set(self.stacks) == \
                set(self.processing) == \
                set(self.nannies) == \
                set(self.worker_queues))

    def diagnostic_resources(self, n=100):
        now = datetime.now()
        workers = {(ip, nport): (ip, wport)
                   for (ip, wport), nport in self.nannies.items()}
        logs = {workers[nanny]: list(log)[-n:]
                for nanny, log in self.resource_logs.items()}
        return {worker: {'cpu': [d['cpu_percent'] for d in log],
                         'memory': [d['memory_percent'] for d in log],
                         'time': [(d['timestamp'] - now).total_seconds()
                                  for d in log]}
                for worker, log in logs.items()}

    @gen.coroutine
    def feed(self, stream, function=None, setup=None, teardown=None, interval=1, **kwargs):
        state = setup(self) if setup else None
        if isinstance(state, gen.Future):
            state = yield state
        try:
            while True:
                if state is None:
                    response = function(self)
                else:
                    response = function(self, state)
                yield write(stream, response)
                yield gen.sleep(interval)
        except (OSError, IOError, StreamClosedError):
            if teardown:
                teardown(self, state)

    def get_who_has(self, stream, keys=None):
        if keys is not None:
            return {k: self.who_has[k] for k in keys}
        else:
            return self.who_has

    def get_has_what(self, stream, keys=None):
        if keys is not None:
            return {k: self.has_what[k] for k in keys}
        else:
            return self.has_what

    def get_ncores(self, stream, addresses=None):
        if addresses is not None:
            return {k: self.ncores.get(k, None) for k in addresses}
        else:
            return self.ncores

    @gen.coroutine
    def broadcast(self, stream, msg=None):
        """ Broadcast message to workers, return all results """
        workers = list(self.ncores)
        results = yield All([send_recv(ip=ip, port=port, close=True, **msg)
                             for ip, port in workers])
        raise Return(dict(zip(workers, results)))


def decide_worker(dependencies, stacks, who_has, restrictions,
                  loose_restrictions, nbytes, key):
    """ Decide which worker should take task

    >>> dependencies = {'c': {'b'}, 'b': {'a'}}
    >>> stacks = {('alice', 8000): ['z'], ('bob', 8000): []}
    >>> who_has = {'a': {('alice', 8000)}}
    >>> nbytes = {'a': 100}
    >>> restrictions = {}
    >>> loose_restrictions = set()

    We choose the worker that has the data on which 'b' depends (alice has 'a')

    >>> decide_worker(dependencies, stacks, who_has, restrictions,
    ...               loose_restrictions, nbytes, 'b')
    ('alice', 8000)

    If both Alice and Bob have dependencies then we choose the less-busy worker

    >>> who_has = {'a': {('alice', 8000), ('bob', 8000)}}
    >>> decide_worker(dependencies, stacks, who_has, restrictions,
    ...               loose_restrictions, nbytes, 'b')
    ('bob', 8000)

    Optionally provide restrictions of where jobs are allowed to occur

    >>> restrictions = {'b': {'alice', 'charile'}}
    >>> decide_worker(dependencies, stacks, who_has, restrictions,
    ...               loose_restrictions, nbytes, 'b')
    ('alice', 8000)

    If the task requires data communication, then we choose to minimize the
    number of bytes sent between workers. This takes precedence over worker
    occupancy.

    >>> dependencies = {'c': {'a', 'b'}}
    >>> who_has = {'a': {('alice', 8000)}, 'b': {('bob', 8000)}}
    >>> nbytes = {'a': 1, 'b': 1000}
    >>> stacks = {('alice', 8000): [], ('bob', 8000): []}

    >>> decide_worker(dependencies, stacks, who_has, {}, set(), nbytes, 'c')
    ('bob', 8000)
    """
    deps = dependencies[key]
    workers = frequencies(w for dep in deps
                            for w in who_has[dep])
    if not workers:
        workers = stacks
    if key in restrictions:
        r = restrictions[key]
        workers = {w for w in workers if w[0] in r}  # TODO: nonlinear
        if not workers:
            workers = {w for w in stacks if w[0] in r}
            if not workers:
                if key in loose_restrictions:
                    return decide_worker(dependencies, stacks, who_has,
                                         {}, set(), nbytes, key)
                else:
                    raise ValueError("Task has no valid workers", key, r)
    if not workers or not stacks:
        raise ValueError("No workers found")

    commbytes = {w: sum(nbytes[k] for k in dependencies[key]
                                   if w not in who_has[k])
                 for w in workers}

    minbytes = min(commbytes.values())

    workers = {w for w, nb in commbytes.items() if nb == minbytes}
    worker = min(workers, key=lambda w: len(stacks[w]))
    return worker


def update_state(dsk, dependencies, dependents, held_data,
                 who_has, in_play,
                 waiting, waiting_data, new_dsk, new_keys):
    """ Update state given new dask graph and output keys

    This should operate in linear time relative to the size of edges of the
    added graph.  It assumes that the current runtime state is valid.
    """
    dsk.update(new_dsk)
    if not isinstance(new_keys, set):
        new_keys = set(new_keys)

    for key in new_dsk:  # add dependencies/dependents
        if key in dependencies:
            continue

        task = new_dsk[key]
        deps = _deps(dsk, task) + _deps(held_data, task)
        dependencies[key] = set(deps)

        for dep in deps:
            if dep not in dependents:
                dependents[dep] = set()
            dependents[dep].add(key)

        if key not in dependents:
            dependents[key] = set()

    exterior = keys_outside_frontier(dsk, dependencies, new_keys, in_play)
    in_play |= exterior
    for key in exterior:
        deps = dependencies[key]
        waiting[key] = {d for d in deps if not (d in who_has and who_has[d])}
        for dep in deps:
            if dep not in waiting_data:
                waiting_data[dep] = set()
            waiting_data[dep].add(key)

        if key not in waiting_data:
            waiting_data[key] = set()

    held_data |= new_keys

    return {'dsk': dsk,
            'dependencies': dependencies,
            'dependents': dependents,
            'held_data': held_data,
            'waiting': waiting,
            'waiting_data': waiting_data}


def validate_state(dependencies, dependents, waiting, waiting_data,
        in_memory, stacks, processing, finished_results, released, in_play,
        allow_overlap=False, **kwargs):
    """ Validate a current runtime state

    This performs a sequence of checks on the entire graph, running in about
    linear time.  This raises assert errors if anything doesn't check out.

    See Also
    --------
    heal: fix a broken runtime state
    """
    in_stacks = {k for v in stacks.values() for k in v}
    in_processing = {k for v in processing.values() for k in v}
    keys = {key for key in dependents if not dependents[key]}

    @memoize
    def check_key(key):
        """ Validate a single key, recurse downards """
        val = sum([key in waiting,
                   key in in_stacks,
                   key in in_processing,
                   key in in_memory,
                   key in released])
        if allow_overlap:
            assert val >= 1
        else:
            assert val == 1

        assert (key in released) != (key in in_play)

        if not all(map(check_key, dependencies[key])):# Recursive case
            assert False

        if key in in_memory:
            assert not any(key in waiting.get(dep, ())
                           for dep in dependents.get(key, ()))
            assert not waiting.get(key)

        if key in in_stacks or key in in_processing:
            assert all(dep in in_memory for dep in dependencies[key])
            assert not waiting.get(key)

        if finished_results is not None:
            if key in finished_results:
                assert key in in_memory
                assert key in keys

            if key in keys and key in in_memory:
                assert key in finished_results

        return True

    assert all(map(check_key, keys))


def keys_outside_frontier(dsk, dependencies, keys, frontier):
    """ All keys required by terminal keys within graph up to frontier

    Given:

    1. A graph
    2. A set of desired keys
    3. A frontier/set of already-computed (or already-about-to-be-computed) keys

    Find all keys necessary to compute the desired set that are outside of the
    frontier.

    Parameters
    ----------
    dsk: dict
    keys: iterable of keys
    frontier: set of keys

    Examples
    --------
    >>> f = lambda:1
    >>> dsk = {'x': 1, 'a': 2, 'y': (f, 'x'), 'b': (f, 'a'), 'z': (f, 'b', 'y')}
    >>> dependencies, dependents = get_deps(dsk)
    >>> keys = {'z', 'b'}
    >>> frontier = {'y', 'a'}
    >>> list(sorted(keys_outside_frontier(dsk, dependencies, keys, frontier)))
    ['b', 'z']
    """
    assert isinstance(keys, set)
    assert isinstance(frontier, set)
    stack = list(keys - frontier)
    result = set()
    while stack:
        x = stack.pop()
        if x in result or x in frontier:
            continue
        result.add(x)
        stack.extend(dependencies[x])

    return result


def heal(dependencies, dependents, in_memory, stacks, processing, waiting,
        waiting_data, **kwargs):
    """ Make a possibly broken runtime state consistent

    Sometimes we lose intermediate values.  In these cases it can be tricky to
    rewind our runtime state to a point where it can again proceed to
    completion.  This function edits runtime state in place to make it
    consistent.  It outputs a full state dict.

    This function gets run whenever something bad happens, like a worker
    failure.  It should be idempotent.
    """
    outputs = {key for key in dependents if not dependents[key]}

    rev_stacks = {k: v for k, v in reverse_dict(stacks).items() if v}
    rev_processing = {k: v for k, v in reverse_dict(processing).items() if v}

    waiting_data.clear()
    waiting.clear()
    finished_results = set()

    def remove_from_stacks(key):
        if key in rev_stacks:
            for worker in rev_stacks.pop(key):
                stacks[worker].remove(key)

    def remove_from_processing(key):
        if key in rev_processing:
            for worker in rev_processing.pop(key):
                processing[worker].remove(key)

    new_released = set(dependents)

    accessible = set()

    @memoize
    def make_accessible(key):
        if key in new_released:
            new_released.remove(key)
        if key in in_memory:
            return

        for dep in dependencies[key]:
            make_accessible(dep)  # recurse

        waiting[key] = {dep for dep in dependencies[key]
                            if dep not in in_memory}

    for key in outputs:
        make_accessible(key)

    waiting_data.update({key: {dep for dep in dependents[key]
                              if dep not in new_released
                              and dep not in in_memory}
                    for key in dependents
                    if key not in new_released})

    def unrunnable(key):
        return (key in new_released
             or key in in_memory
             or waiting.get(key)
             or not all(dep in in_memory for dep in dependencies[key]))

    for key in list(filter(unrunnable, rev_stacks)):
        remove_from_stacks(key)

    for key in list(filter(unrunnable, rev_processing)):
        remove_from_processing(key)

    for key in list(rev_stacks) + list(rev_processing):
        if key in waiting:
            assert not waiting[key]
            del waiting[key]

    finished_results = {key for key in outputs if key in in_memory}

    in_play = (in_memory
            | set(waiting)
            | (set.union(*processing.values()) if processing else set())
            | set(concat(stacks.values())))

    output = {'keys': outputs,
             'dependencies': dependencies, 'dependents': dependents,
             'waiting': waiting, 'waiting_data': waiting_data,
             'in_memory': in_memory, 'processing': processing, 'stacks': stacks,
             'finished_results': finished_results, 'released': new_released,
             'in_play': in_play}
    validate_state(**output)
    return output


_round_robin = [0]


def assign_many_tasks(dependencies, waiting, keyorder, who_has, stacks,
        restrictions, loose_restrictions, nbytes, keys):
    """ Assign many new ready tasks to workers

    Often at the beginning of computation we have to assign many new leaves to
    several workers.  This initial seeding of work can have dramatic effects on
    total runtime.

    This takes some typical state variables (dependencies, waiting, keyorder,
    who_has, stacks) as well as a list of keys to assign to stacks.

    This mutates waiting and stacks in place and returns a dictionary,
    new_stacks, that serves as a diff between the old and new stacks.  These
    new tasks have yet to be put on worker queues.
    """
    leaves = list()  # ready tasks without data dependencies
    ready = list()   # ready tasks with data dependencies
    new_stacks = defaultdict(list)

    for k in keys:
        assert not waiting.pop(k)
        if not dependencies[k] and k not in restrictions:
            leaves.append(k)
        else:
            ready.append(k)

    if not stacks:
        raise ValueError("No workers found")

    leaves = sorted(leaves, key=keyorder.get)

    workers = list(stacks)

    k = _round_robin[0] % len(workers)
    workers = workers[k:] + workers[:k]
    _round_robin[0] += 1

    k = int(ceil(len(leaves) / len(workers)))
    for i, worker in enumerate(workers):
        keys = leaves[i*k: (i + 1)*k][::-1]
        new_stacks[worker].extend(keys)
        stacks[worker].extend(keys)

    for key in ready:
        worker = decide_worker(dependencies, stacks, who_has, restrictions,
                loose_restrictions, nbytes, key)
        new_stacks[worker].append(key)
        stacks[worker].append(key)

    return new_stacks


def heal_missing_data(dsk, dependencies, dependents, held_data,
                      in_memory, in_play,
                      waiting, waiting_data, missing):
    """ Return to healthy state after discovering missing data

    When we identify that we're missing certain keys we rewind runtime state to
    evaluate those keys.
    """
    logger.debug("Healing missing: %s", missing)
    for key in missing:
        if key in in_play:
            in_play.remove(key)

    def ensure_key(key):
        if key in in_play:
            return
        for dep in dependencies[key]:
            ensure_key(dep)
            waiting_data[dep].add(key)
        waiting[key] = {dep for dep in dependencies[key] if dep not in in_memory}
        logger.debug("Added key to waiting: %s", key)
        waiting_data[key] = {dep for dep in dependents[key] if dep in in_play
                                                    and dep not in in_memory}
        in_play.add(key)

    for key in missing:
        ensure_key(key)

    assert set(missing).issubset(in_play)


def cover_aliases(dsk, new_keys):
    """ Replace aliases with calls to identity

    Warning: operates in place

    >>> dsk = {'x': 1, 'y': 'x'}
    >>> cover_aliases(dsk, ['y'])  # doctest: +SKIP
    {'x': 1, 'y': (<function identity ...>, 'x')}
    """
    for key in new_keys:
        try:
            if dsk[key] in dsk:
                dsk[key] = (identity, dsk[key])
        except TypeError:
            pass

    return dsk


def execute_task(task):
    """ Evaluate a nested task """
    if istask(task):
        func, args = task[0], task[1:]
        return func(*map(execute_task, args))
    else:
        return task
