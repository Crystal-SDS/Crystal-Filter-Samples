from crystal_filter_middleware.filters.abstract_filter import AbstractFilter, FilterIter
from swift.common.swob import Request, Response
from threading import Semaphore
import hashlib
import time
import os

ENABLE_CACHE = True
# Cache size limit in bytes
CACHE_MAX_SIZE = 200*1024*1024*1024
available_policies = {"LRU", "LFU"}
CACHE_PATH = "/tmp/cache/"


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):  # @NoSelf
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


# crystal_cache_control.CacheControl has 3 points of interception:
# 1. pre-get: to check if the file is in the cache
# 2. post-get: to store an existing file in the cache
# 3. pre-put: to store a new file in the cache
class CacheControl(AbstractFilter):

    __metaclass__ = Singleton

    def __init__(self, global_conf, filter_conf, logger):
        super(CacheControl, self).__init__(global_conf, filter_conf, logger)
        self.cache = BlockCache()

    def _apply_filter(self, req_resp, data_iter, parameters):
        method = req_resp.environ['REQUEST_METHOD']

        if method == 'GET':
            return self._get_object(req_resp, data_iter)

        elif method == 'PUT':
            return self._put_object(req_resp, data_iter)

    def _get_object(self, req_resp, crystal_iter):

        resp_headers = {}

        if isinstance(req_resp, Request):

            # CHECK IF FILE IS IN CACHE
            if os.path.exists(CACHE_PATH):

                object_path = req_resp.environ['PATH_INFO']
                object_id = (hashlib.md5(object_path).hexdigest())

                object_id, object_size, object_etag = self.cache.access_cache("GET", object_id)

                if object_id:
                    self.logger.info('SDS Cache Filter - Object '+object_path+' in cache')
                    resp_headers = {}
                    resp_headers['content-length'] = str(object_size)
                    resp_headers['etag'] = object_etag

                    cached_object = open(CACHE_PATH+object_id, 'r')

                    # TODO: Return headers if necessary
                    return cached_object

        elif isinstance(req_resp, Response):

            if os.path.exists(CACHE_PATH):
                object_path = req_resp.environ['PATH_INFO']
                object_size = int(req_resp.headers.get('Content-Length', ''))
                object_etag = req_resp.headers.get('ETag', '')
                object_id = (hashlib.md5(object_path).hexdigest())

                to_evict = self.cache.access_cache("PUT", object_id, object_size, object_etag)

                for object_id in to_evict:
                    os.remove(CACHE_PATH + object_id)

                self.logger.info('SDS Cache Filter (POST-GET) - Object ' + object_path + ' stored in cache with ID: ' + object_id)

                self.cached_object = open(CACHE_PATH + object_id, 'w')
                return FilterIter(crystal_iter, 10, self._filter_put)

        return req_resp.environ['wsgi.input']

    def _put_object(self, request, crystal_iter):

        if os.path.exists(CACHE_PATH):
            object_path = request.environ['PATH_INFO']
            object_size = int(request.headers.get('Content-Length', ''))
            object_etag = request.headers.get('ETag', '')
            object_id = (hashlib.md5(object_path).hexdigest())

            to_evict = self.cache.access_cache("PUT", object_id, object_size, object_etag)

            for object_id in to_evict:
                os.remove(CACHE_PATH+object_id)

            self.logger.info('SDS Cache Filter - Object '+object_path+' stored in cache with ID: '+object_id)

            self.cached_object = open(CACHE_PATH+object_id, 'w')
            return FilterIter(crystal_iter, 10, self._filter_put)

    def _filter_put(self, chunk):
        self.cached_object.write(chunk)
        return chunk


class CacheObjectDescriptor(object):

    def __init__(self, block_id, size, etag):
        self.block_id = block_id
        self.last_access = time.time()
        self.get_hits = 0
        self.put_hits = 0
        self.num_accesses = 0
        self.size = size
        self.etag = etag

    def get_hit(self):
        self.get_hits += 1
        self.hit()

    def put_hit(self):
        self.put_hits += 1
        self.hit()

    def hit(self):
        self.last_access = time.time()
        self.num_accesses += 1


class BlockCache(object):

    def __init__(self):
        # This will contain the actual data of each block
        self.descriptors_dict = {}
        # Structure to store the cache metadata of each block
        self.descriptors = []
        # Cache statistics
        self.get_hits = 0
        self.put_hits = 0
        self.misses = 0
        self.evictions = 0
        self.reads = 0
        self.writes = 0
        self.cache_size_bytes = 0

        # Eviction policy
        self.policy = "LFU"
        # Synchronize shared cache content
        self.semaphore = Semaphore()

    def access_cache(self, operation='PUT', block_id=None, block_data=None, etag=None):
        result = None
        if ENABLE_CACHE:
            self.semaphore.acquire()
            if operation == 'PUT':
                result = self._put(block_id, block_data, etag)
            elif operation == 'GET':
                result = self._get(block_id)
            else:
                raise Exception("Unsupported cache operation" + operation)
            # Sort descriptors based on eviction policy order
            self._sort_descriptors()
            self.semaphore.release()
        return result

    def _put(self, block_id, block_size, etag):
        self.writes += 1
        to_evict = []
        # Check if the cache is full and if the element is new
        if CACHE_MAX_SIZE <= (self.cache_size_bytes + block_size) and block_id not in self.descriptors_dict:
            # Evict as many files as necessary until having enough space for new one
            while (CACHE_MAX_SIZE <= (self.cache_size_bytes + block_size)):
                # Get the last element ordered by the eviction policy
                self.descriptors, evicted = self.descriptors[:-1], self.descriptors[-1]
                # Reduce the size of the cache
                self.cache_size_bytes -= evicted.size
                # Increase evictions count and add to
                self.evictions += 1
                to_evict.append(evicted.block_id)
                # Remove from evictions dict
                del self.descriptors_dict[evicted.block_id]

        if block_id in self.descriptors_dict:
            descriptor = self.descriptors_dict[block_id]
            self.descriptors_dict[block_id].size = block_size
            self.descriptors_dict[block_id].etag = etag
            descriptor.put_hit()
            self.put_hits += 1
        else:
            # Add the new element to the cache
            descriptor = CacheObjectDescriptor(block_id, block_size, etag)
            self.descriptors.append(descriptor)
            self.descriptors_dict[block_id] = descriptor
            self.cache_size_bytes += block_size

        assert len(self.descriptors) == len(self.descriptors_dict.keys()) ==\
            len(self.descriptors_dict.keys()), "Unequal length in cache data structures"

        return to_evict

    def _get(self, block_id):
        self.reads += 1
        if block_id in self.descriptors_dict:
            self.descriptors_dict[block_id].get_hit()
            self.get_hits += 1
            return block_id, self.descriptors_dict[block_id].size, self.descriptors_dict[block_id].etag
        self.misses += 1
        return None, 0, ''

    def _sort_descriptors(self):
        # Order the descriptor list depending on the policy
        if self.policy == "LRU":
            self.descriptors.sort(key=lambda desc: desc.last_access, reverse=True)
        elif self.policy == "LFU":
            self.descriptors.sort(key=lambda desc: desc.get_hits, reverse=True)
        else:
            raise Exception("Unsupported caching policy.")

    def write_statistics(self):
        if ENABLE_CACHE:
            self.cache_state()

    def cache_state(self):
        print "CACHE GET HITS: ", self.get_hits
        print "CACHE PUT HITS: ", self.put_hits
        print "CACHE MISSES: ", self.misses
        print "CACHE EVICTIONS: ", self.evictions
        print "CACHE READS: ", self.reads
        print "CACHE WRITES: ", self.writes
        print "CACHE SIZE: ", self.cache_size_bytes

        for descriptor in self.descriptors:
            print "Object: ", descriptor.block_id, descriptor.last_access, descriptor.get_hits, descriptor.put_hits, descriptor.num_accesses, descriptor.size
