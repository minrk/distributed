from __future__ import print_function, division, absolute_import

from datetime import datetime, timedelta
import logging
from multiprocessing import Process, Queue, queues
import os
import shutil
import tempfile

from tornado.ioloop import IOLoop
from tornado import gen

from .core import Server, rpc, write
from .utils import get_ip


logger = logging.getLogger(__name__)

class Nanny(Server):
    """ A process to manage worker processes

    The nanny spins up Worker processes, watches then, and kills or restarts
    them as necessary.
    """
    def __init__(self, center_ip, center_port, ip=None,
                ncores=None, loop=None, local_dir=None, **kwargs):
        self.ip = ip or get_ip()
        self.worker_port = None
        self.ncores = ncores
        self.local_dir = local_dir
        self.worker_dir = ''
        self.status = None
        self.process = None
        self.loop = loop or IOLoop.current()
        self.center = rpc(ip=center_ip, port=center_port)

        handlers = {'instantiate': self.instantiate,
                    'kill': self._kill,
                    'terminate': self._close,
                    'monitor_resources': self.monitor_resources}

        super(Nanny, self).__init__(handlers, **kwargs)

    @gen.coroutine
    def _start(self, port=0):
        """ Start nanny, start local process, start watching """
        self.listen(port)
        logger.info('Start Nanny at:             %s:%d', self.ip, self.port)
        yield self.instantiate()
        self.loop.add_callback(self._watch)
        assert self.worker_port
        self.status = 'running'

    @gen.coroutine
    def _kill(self, stream=None, timeout=5):
        """ Kill the local worker process

        Blocks until both the process is down and the center is properly
        informed
        """
        while not self.worker_port:
            yield gen.sleep(0.1)

        if self.process is not None:
            try:
                result = yield gen.with_timeout(timedelta(seconds=timeout),
                            self.center.unregister(address=self.worker_address))
                if result != b'OK':
                    logger.critical("Unable to unregister with center %s. "
                            "Nanny: %s, Worker: %s", result, self.address,
                            self.worker_address)
                else:
                    logger.info("Unregister worker %s:%d from center",
                                self.ip, self.worker_port)
            except gen.TimeoutError:
                logger.info("Nanny %s:%d failed to unregister worker %s:%d",
                        self.ip, self.port, self.ip, self.worker_port,
                        exc_info=True)
            self.process.terminate()
            self.process = None
            logger.info("Nanny %s:%d kills worker process %s:%d",
                        self.ip, self.port, self.ip, self.worker_port)
            self.cleanup()
        raise gen.Return(b'OK')

    @gen.coroutine
    def instantiate(self, stream=None):
        """ Start a local worker process

        Blocks until the process is up and the center is properly informed
        """
        if self.process and self.process.is_alive():
            raise ValueError("Existing process still alive. Please kill first")
        q = Queue()
        self.process = Process(target=run_worker,
                               args=(q, self.ip, self.center.ip,
                                     self.center.port, self.ncores,
                                     self.port, self.local_dir))
        self.process.daemon = True
        self.process.start()
        while True:
            try:
                msg = q.get_nowait()
                if isinstance(msg, Exception):
                    raise msg
                self.worker_port = msg['port']
                assert self.worker_port
                self.worker_dir = msg['dir']
                break
            except queues.Empty:
                yield gen.sleep(0.1)
        logger.info("Nanny %s:%d starts worker process %s:%d",
                    self.ip, self.port, self.ip, self.worker_port)
        raise gen.Return(b'OK')

    def cleanup(self):
        if os.path.exists(self.worker_dir):
            shutil.rmtree(self.worker_dir)
        self.worker_dir = None

    @gen.coroutine
    def _watch(self, wait_seconds=0.10):
        """ Watch the local process, if it dies then spin up a new one """
        while True:
            if self.status == 'closed':
                yield self._close()
                break
            if self.process and not self.process.is_alive():
                logger.info("Discovered failed worker.  Restarting")
                self.cleanup()
                yield self.center.unregister(address=self.worker_address)
                yield self.instantiate()
            else:
                yield gen.sleep(wait_seconds)

    @gen.coroutine
    def _close(self, stream=None, timeout=5):
        """ Close the nanny process, stop listening """
        logger.info("Closing Nanny at %s:%d", self.ip, self.port)
        yield self._kill(timeout=timeout)
        self.center.close_streams()
        self.stop()
        self.status = 'closed'
        raise gen.Return(b'OK')

    @property
    def address(self):
        return (self.ip, self.port)

    @property
    def worker_address(self):
        return (self.ip, self.worker_port)

    def resource_collect(self):
        try:
            import psutil
        except ImportError:
            return {}
        p = psutil.Process(self.process.pid)
        return {'timestamp': datetime.now(),
                'cpu_percent': psutil.cpu_percent(),
                'status': p.status(),
                'memory_percent': p.memory_percent(),
                'memory_info_ex': p.memory_info_ex(),
                'disk_io_counters': psutil.disk_io_counters(),
                'net_io_counters': psutil.net_io_counters()}

    @gen.coroutine
    def monitor_resources(self, stream, interval=1):
        while not stream.closed():
            if self.process:
                yield write(stream, self.resource_collect())
            yield gen.sleep(interval)


def run_worker(q, ip, center_ip, center_port, ncores, nanny_port,
        local_dir):
    """ Function run by the Nanny when creating the worker """
    from distributed import Worker
    from tornado.ioloop import IOLoop
    IOLoop.clear_instance()
    loop = IOLoop()
    loop.make_current()
    worker = Worker(center_ip, center_port, ncores=ncores, ip=ip,
                    nanny_port=nanny_port, local_dir=local_dir)

    @gen.coroutine
    def start():
        try:
            yield worker._start()
        except Exception as e:
            logger.exception(e)
            q.put(e)
        else:
            assert worker.port
            q.put({'port': worker.port, 'dir': worker.local_dir})

    loop.add_callback(start)
    loop.start()
