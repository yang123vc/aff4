import pyaff4
import pdb
import time

time.sleep(1)

urn_base = 'http://www.google.com/'

cache = pyaff4.Cache(max_cache_size = 10)

for x in range(0,15):
    urn = urn_base + "%s.html" % x
    r = pyaff4.RDFURN()
    r.set(urn)
    cache.put("%s" % x, r)
    del r

print cache.cache_size, cache.max_cache_size

## Get an item
try:
    a = cache.get("1")
except KeyError:
    a = cache.get("10")

print a.value

print cache.cache_size, cache.max_cache_size

print "Testing multiple values with the same key"
## Now test setting multiple values for a single key
for x in range(0,15):
    urn = urn_base + "%s.html" % x
    r = pyaff4.RDFURN()
    r.set(urn)
    cache.put(urn_base, r)
    del r

iter = cache.iter(urn_base)
while iter:
    print urn_base, cache.next(iter).value
