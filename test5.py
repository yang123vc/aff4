"""
This test tests the Map stream type's ability to reference more than
one target stream and repeat. This can be used to create a map for
RAID components.

We use the raid5 test set from PyFlag's testimages corpus.

We first create a FIF archive with 3 streams one for each image. Then
we build a mapping function. Finally to test the reassembly we use SK
to copy a file out of the logical disk, and check its hash.
"""

import fif,os,md5,sk, sys

FILENAME = "./samples/raid5.zip"
targets = []

def build_file():
    BASEDIR = "./images/raid/linux/d%d.dd"

    for i in range(1,4):
        filename = BASEDIR % i
        fd = fiffile.create_stream_for_writing(local_name=filename)
        targets.append(fd.getId())
        infd = open(filename)
        while 1:
            data = infd.read(fd.chunksize)
            if len(data)==0: break

            fd.write(data)

        fd.close()

def make_raid_map():
    blocksize = 64 * 1024
    properties = fif.properties()
    properties['aff2:target'] = targets[0]
    properties['aff2:target'] = targets[1]
    properties['aff2:target'] = targets[2]
    properties['aff2:image_period'] = blocksize * 3
    properties['aff2:file_period'] = blocksize * 6
    print properties    
    new_stream = fiffile.create_stream_for_writing(local_name="RAID",
                                                   stream_type='aff2:Map',
                                                   properties = properties,
                                                   target=targets)
    
    new_stream.add_point(0,            0,              1)
    new_stream.add_point(1 * blocksize,0 ,             0)
    new_stream.add_point(2 * blocksize,1 * blocksize , 2)
    new_stream.add_point(3 * blocksize,1 * blocksize , 1)
    new_stream.add_point(4 * blocksize,2 * blocksize , 0)
    new_stream.add_point(5 * blocksize,2 * blocksize , 2)

    new_stream.size = 5242880 * 2
    new_stream.save_map()
    new_stream.close()

if 1:
    try:
        os.unlink(FILENAME)
    except: pass

    fiffile = fif.FIFFile()
    fiffile.create_new_volume(FILENAME)
    build_file()
    make_raid_map()
    
    fiffile.close()
else:
    fiffile = fif.FIFFile([FILENAME])

fd = fiffile.open_stream_by_name("RAID")

fs = sk.skfs(fd)
f = fs.open(inode='13')
outfd = open("test3.jpg","w")
m = md5.new()
while 1:
    data = f.read(1024*1024)
    if len(data)==0: break
    outfd.write(data)
    m.update(data)

print m.hexdigest()
assert(m.hexdigest() == 'a4b4ea6e28ce5d6cce3bcff7d132f162')
