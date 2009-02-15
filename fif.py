import zipfile, struct, zlib, re
import Queue, threading, time, binascii
import bisect, os, uuid

## The version of this implementation of FIF
VERSION = "FIF 1.0"

## Alas python zlib grabs the GIL so threads dont give much. This
## should be fixed soon: http://bugs.python.org/issue4738
## For now we disable threads
NUMBER_OF_THREAD = 2

class Store:
    """ A limited cache implementation """
    def __init__(self, limit=5000000):
        ## This is the maximum amount of memory we can use up:
        self.limit = limit
        self.cache = {}
        self.seq = []
        self.size = 0
        self.expired = 0

    def put(self, key, obj):
        self.cache[key] = obj
        self.size += len(obj)
        
        self.seq.append(key)

        while self.size >= self.limit:
            key = self.seq.pop(0)
            self.size -= len(self.cache[key])
            self.expired += 1
            del self.cache[key]

    def get(self, key):
        return self.cache[key]

    def expire(self, key):
        try:
            del self.cache[key]
        except KeyError:
            pass

class FIFFile(zipfile.ZipFile):
    """ A FIF file is just a Zip file which follows some rules.

    FIF files are made up of a series of volumes. Volumes are treated
    as parts of a single logical FIF file by consolidating all their
    Central Directory (CD) entries according to the following rules:

    - Multiple CD entries for the same file path are overridden by the
    latest entry according to the last modfied date and time fields in
    the CD.

    - CD Entries with compressed_size and uncompr_size of zero are
    considered to be deleted and removed from the combined CD.

    - Each volume MUST have a properties file at the top level. This
    properties file MUST have the same UUID attribute for volumes in
    the same logical FIF fileset. UUIDs are generated using RFC 4122.

    In this way we are able to extend and modify files within the FIF
    fileset by creating additional volumes. Use fifrepack to merge all
    volumes into a single volume and repack the volume to remove old
    files.
    """
    ## Each FIF file has a unique UUID
    UUID = None

    ## This is the file object which is currently being written to.
    fp = None

    ## This is an estimate of our current size
    size = 0

    ## This is a write lock for the current FIF file
    wlock = None

    def __init__(self, filenames=[]):
        ## This is an index of all files in the archive set (tuples of
        ## ZipInfo and ZipFile index)
        self.file_offsets = {}
        self.Store = Store()
        self.zipfiles = []

        ## Initialise our baseclass for writing - writing is mainly
        ## handled by our base class, while reading is done directly
        ## using the file_offsets index.
        zipfile.ZipFile.__init__(self, None, mode='w',
                                 compression=zipfile.ZIP_DEFLATED,
                                 allowZip64=True)
        
        try:
            ## Its a file like object
            for f in filenames:
                f.seek
                f.read
                f.write
                self.zipfiles.append(f)
        except AttributeError:
            ## Its a string
            self.zipfiles.append(open(f,'r+b'))

        for fileobj in self.zipfiles:
            ## We parse out the CD of each file and build an index
            zf = zipfile.ZipFile(fileobj,
                                 mode='r', allowZip64=True)
            
            for zinfo in zf.infolist():
                filename = zinfo.filename
                self.update_index(fileobj, zinfo)
                
                ## Check the UUID:
                if filename=='properties':
                    props = self.parse_properties_from_string(zf.read("properties"))
                    if self.UUID:
                        assert(self.UUID == props['UUID'],
                               "File %s does not have the right UUID" % filename)
                    else:
                        self.UUID = props['UUID']

    def update_index(self, fileobj, zinfo):
        """ Updates the offset index to point within the zip file we
        need to read from. This is done so we dont need to reparse the
        zip headers all the time (probably wont be needed in the C
        implementation - just that python is abit slow)
        """
        try:
            ## Update the index with the more recent zinfo
            row = self.file_offsets[fileobj.name]
            if row[4] > zinfo.date_time:
                return
        except KeyError:
            pass

        ## Work out where the compressed file should start
        offset = zinfo.header_offset + len(zinfo.FileHeader())

        self.file_offsets[zinfo.filename] = ( fileobj, offset,
                                              zinfo.compress_size, zinfo.compress_type,
                                              zinfo.date_time)            

    def open_stream_for_writing(self, stream_name):
        ## check the lock:
        if self.wlock:
            raise IOError("Zipfile is currently locked for writing member %s" % self.wlock)

        self.wlock = stream_name

        return ZipFileStream(self, stream_name)
        return self.wlock

    def writestr(self, member_name, data):
        """ This method write a component to the archive. """
        if not self.fp:
            raise RuntimeError("Trying to write to archive but no archive was set - did you need to call create_new_volume() or append_volume() first?")

        ## FIXME - implement digital signatures here
        ## Call our base class to actually do the writing
        zipfile.ZipFile.writestr(self, member_name, data)

        ## We can actually update the index here so the member can be
        ## available for reading immediately:
        zinfo  = self.getinfo(member_name)
        self.update_index(self.fp, zinfo)

        ## invalidate the cache if its in there
        self.Store.expire(member_name)

        ## How big are we?
        self.size = self.fp.tell()

    def read_member(self, filename):
        """ Read a file from the archive. We do memory caching for
        speed, and read the file directly from the fileobj.

        Note that we read the whole file here at once. This is only
        suitable for small files as large files will cause us to run
        out of memory. For larger files you should just call
        open_member() and then read().
        """
        try: 
            return self.Store.get(filename)
        except KeyError:
            ## Lookup the index for speed:
            zf, offset, length, compress_type, date_time = \
                     self.file_offsets[filename]

            zf.seek(offset)
            bytes = zf.read(length)

            ## Decompress if needed
            if compress_type == zipfile.ZIP_DEFLATED:
                dc = zlib.decompressobj(-15)
                bytes = dc.decompress(bytes)
                # need to feed in unused pad byte so that zlib won't choke
                ex = dc.decompress('Z') + dc.flush()
                if ex:
                    bytes = bytes + ex
                    
            self.Store.put(filename, bytes)
            return bytes

    def parse_properties_from_string(self, string):
        properties = {}
        for line in string.splitlines():
            k,v = line.split("=",1)
            properties[k] = v

        return properties

    def append_volume(self, filename):
        """ Append to this volume """
        ## Close the current file if needed:
        self.close()

        ## We want to set this filename as the current file to write
        ## on. Therefore, we need to reload the CD from that file so
        ## we can re-write the modified CD upon a flush() or close().
        for z in self.zipfiles:
            if z.name == filename:
                ## There it is:
                self.fp = z
                ## Rescan the CD
                self._RealGetContents()
                return
            
        raise RuntimeError("Cant append to file %s - its not part of the FIF set" % filename)

    def create_new_volume(self, filename):
        """ Creates a new volume called filename. """
        ## Close the current file if needed:
        self.close()

        ## Were we given a filename or a file like object?
        try:
            filename.seek
            filename.read
            filename.write
        except AttributeError:
            try:
                filename = open(filename,'r+b')
                raise RuntimeError("The file %s already exists... I dont want to over write it so you need to remove it first!!!" % filename)
            except IOError:
                filename = open(filename,'w+b')

        ## Remember it
        self.zipfiles.append(filename)

        ## We now will be operating on this - reinitialise ourselves
        ## for writing on it (this will clear out of CD list):
        zipfile.ZipFile.__init__(self, filename, mode='w', compression=zipfile.ZIP_DEFLATED,
                                 allowZip64 = True)

    def format_properties(self, properties):
        result = ''
        for k,v in properties.items():
            result+="%s=%s\n" % (k,v)

        return result

    def close(self):
        """ Finalise any created volume """
        if self._didModify and self.fp:
            print "Closing FIFFile %s" % self.fp.name
            if not self.UUID:
                self.UUID = uuid.uuid4().__str__()

            self.writestr("properties",
                          self.format_properties(dict(
                UUID = self.UUID,
                )))

            ## Call our base class to close us - this will dump out
            ## the CD
            zipfile.ZipFile.close(self)



    def create_stream_for_writing(self, stream_name, stream_type='Image', **properties):
        """ This method creates a new stream for writing in the
        current FIF archive.

        We essentially instantiate the driver named by stream_type for
        writing and return it.
        """
        stream = types[stream_type]
        return stream(fd=self, stream_name=stream_name, mode='w', **properties)

    def open_stream(self, stream_name):
        """ Opens the specified stream from out FIF set.

        We basically instantiate the right driver and return it.
        """
        properties = self.parse_properties_from_string(
            self.read_member("%s/properties" % stream_name))

        stream = types[properties['type']]
        return stream(fd=self, stream_name=stream_name, mode='r', **properties)

    def stats(self):
        print "Store load %s chunks total %s (expired %s)" % (
            len(self.Store.seq),
            self.Store.size,
            self.Store.expired,
            )

class FIFFD:
    """ A baseclass to facilitate access to FIF Files"""
    def write_properties(self, target):
        """ This returns a formatted properties string. Properties are
        related to the stream. target is a zipfile.
        """
        self.properties.update(dict(size = self.size,
                                    type = self.type,
                                    name = self.stream_name,))
        result = ''
        for k,v in self.properties.items():
            result+="%s=%s\n" % (k,v)

        ## Make the properties file
        filename = "%s/properties" % self.stream_name
        target.writestr(filename, result)

    def seek(self, pos, whence=0):
        if whence==0:
            self.readptr = pos
        elif whence==1:
            self.readptr += pos
        elif whence==2:
            self.readptr = self.size + pos

    def tell(self):
        return self.readptr

    def flush(self):
        pass

    def close(self):
        if self.mode == 'w':
            self.write_properties(self.fd)        
            #self.fd.close()

class ZipFileStream(FIFFD):
    """ This is a file like object which represents a zip file member """
    def __init__(self, parent, member_name):
        ## This is the FIFFile we belong to
        self.parent = parent
        self.readptr = 0
        self.size = 0
        self.zinfo = zipfile.ZipInfo(member_name, date_time=time.gmtime())
        ## We are about to write an unspecified length member
        self.zinfo.compress_size = 0
        self.zinfo.file_size = 0
        self.zinfo.CRC = 0
        self.zinfo.flag_bits = 0x08
        self.zinfo.header_offset = parent.fp.tell()
        ## Write the zinfo header like that
        self.parent.fp.write(self.zinfo.FileHeader())
        self.parent._didModify = True
        self.file_offset = parent.fp.tell()

    def read(self, length=None):
        if length==None: length = sys.maxint

        length = min(length, self.size - self.readptr)
        self.parent.fp.seek(self.readptr + self.file_offset)
        return self.parent.fp.read(length)
        
    def write(self, data):
        self.parent.fp.seek(self.readptr + self.file_offset)
        self.parent.fp.write(data)
        self.zinfo.file_size += len(data)
        self.zinfo.compress_size += len(data)
        self.zinfo.CRC = binascii.crc32(data, self.zinfo.CRC)
        self.size += len(data)
        self.readptr += len(data)
        
    def seek(self, pos, whence=0):
        FIFFD.seek(pos, whence)
        
    def close(self):
        ## Write the data descriptor
        self.parent.fp.write(struct.pack("<lLL", self.zinfo.CRC,
                                         self.zinfo.compress_size,
                                         self.zinfo.file_size,))

        ## Add the new zinfo to the CD
        self.parent.filelist.append(self.zinfo)
        self.parent.NameToInfo[self.zinfo.filename] = self.zinfo

        ## Invalidate cache
        self.parent.Store.expire(self.zinfo.filename)

        print self.zinfo.filename

        ## Add to the read cache
        self.parent.file_offsets[self.zinfo.filename] = (
            self.parent.fp,
            ## Offset to the start of the file (after the header)
            self.file_offset,
            self.zinfo.file_size,
            zipfile.ZIP_STORED,
            self.zinfo.date_time )
        
        ## Remove the write lock from the parent
        self.parent.wlock = False

class Image(FIFFD):
    """ A stream writer for creating a new FIF archive """
    type = "Image"
    size = 0
    
    def __init__(self, mode='w', stream_name='data',
                 attributes=None, threading=False, fd=None, **properties):
        self.fd = fd
        self.mode = mode
        self.stream_name = stream_name
        self.chunk_id = 0
        self.chunksize = int(properties.get('chunksize', 32*1024))
        self.size = int(properties.get('size',0))
        self.threading = threading
        self.readptr = 0
        self.Store = Store()
        self.stream_re = re.compile(r"%s/(\d+)+.dd" % self.stream_name)
        
        ## The following are mandatory properties:
        self.properties = properties
        if threading:
        ## New compression jobs get pushed here
            self.IN_QUEUE = Queue.Queue(NUMBER_OF_THREAD+1)
            
        ## Results get pushed here by the threads
            self.OUT_QUEUE = Queue.Queue(NUMBER_OF_THREAD+1)

        ## Start a couple of threads
            self.threads = [] 
            for i in range(NUMBER_OF_THREAD):
                x = threading.Thread(target = self.compress_chunk_thread)
                x.start()

    def read(self, length):
        result = ''
        length = min(self.size - self.readptr, length)
        while length>0:
            data= self.partial_read(length)
            length -= len(data)
            result += data

        return result
    
    def partial_read(self, length):
        ## which chunk is it?
        chunk_id = self.readptr / self.chunksize
        chunk_offset = self.readptr % self.chunksize
        available_to_read = min(self.chunksize - chunk_offset, length)

        chunk = self.fd.read_member(self.make_chunk_name(chunk_id))
        self.readptr += available_to_read
        return chunk[chunk_offset:chunk_offset+available_to_read]

    def write_properties(self, target):
        self.properties['count'] = self.chunk_id+1
        FIFFD.write_properties(self, target)
            
    def compress_chunk_thread(self):
        """ This function runs forever in a new thread performing the
        compression jobs.
        """
        while 1:
            data, name = self.IN_QUEUE.get(True)
            ## We need to quit when theres nothing
            if name==None: return

            zinfo = zipfile.ZipInfo(filename = name,
                                date_time=time.localtime(time.time())[:6])
            zinfo.file_size = len(data)
            zinfo.CRC = binascii.crc32(data)
            zinfo.compress_type = self.fd.compression
            if zinfo.compress_type == zipfile.ZIP_DEFLATED:
                co = zlib.compressobj(-1,
                                      zlib.DEFLATED, -15)
                data = co.compress(data) + co.flush()
                zinfo.compress_size = len(data)    # Compressed size
            else:
                zinfo.compress_size = zinfo.file_size

            try:
                self.OUT_QUEUE.put( (data, zinfo), True, 1)
            except Queue.Full:
                return

    def check_queues(self):
        """ A function which writes the compressed queues to the file
        """
        try:
            while 1:
                bytes, zinfo = self.OUT_QUEUE.get(False)
                
                zinfo.header_offset = self.fd.fp.tell()    # Start of header bytes
                self.fd._writecheck(zinfo)
                self.fd._didModify = True

                self.fd.fp.write(zinfo.FileHeader())
                self.fd.fp.write(bytes)
                self.fd.fp.flush()
                if zinfo.flag_bits & 0x08:
                    # Write CRC and file sizes after the file data
                    self.fd.fp.write(struct.pack("<lLL", zinfo.CRC, zinfo.compress_size,
                                                 zinfo.file_size))
                self.fd.filelist.append(zinfo)
                self.fd.NameToInfo[zinfo.filename] = zinfo
                
        except Queue.Empty:
            pass

    def make_chunk_name(self, chunk_id):
        return "%s/%08d.dd" % (self.stream_name, chunk_id)
    
    def write_chunk(self, data):
        """ Adds the chunk to the archive """
        assert(len(data)==self.chunksize)
        name = self.make_chunk_name(self.chunk_id)
        if self.threading:
            self.IN_QUEUE.put( (data, name) )
            self.check_queues()
        else:
            self.fd.writestr(name, data)
            self.chunk_id += 1

        self.size += self.chunksize
        
    def close(self):
        """ Finalise the archive """
        if self.threading:
            ## Kill off the threads
            for i in range(NUMBER_OF_THREAD+1):
                self.IN_QUEUE.put((None, None))

            self.check_queues()

        if self.mode=='w':
            self.write_properties(self.fd)        
            #self.fd.close()

class MapDriver(FIFFD):
    """ A Map driver is a read through mapping transformation of the
    target stream to create a new stream.

    We require the stream properties to specify a 'target'. This can
    either be a plain stream name or can begin with 'file://'. In the
    latter case this indicates that we should be opening an external
    file of the specified filename.

    We expect to find a component in the archive called 'map' which
    contains a mapping function. The file should be of the format:

    - lines starting with # are ignored
    
    - other lines have 2 integers seperated by white space. The first
    column is the current stream offset, while the second offset if
    the target stream offset.

    For example:
    0     1000
    1000  4000

    This means that when the current stream is accessed in the range
    0-1000 we fetch bytes 1000-2000 from the target stream, and after
    that we fetch bytes from offset 4000.

    Required properties:
    
    - target%d starts with 0 the number of target (may be specified as
      a URL). e.g. target0, target1, target2

    Optional properties:

    - file_period - number of bytes in the file offset which this map
      repreats on. (Useful for RAID)

    - image_period - number of bytes in the target image each period
      will advance by. (Useful for RAID)
    
    """
    type = "Map"
    
    def __init__(self, fd=None, mode='r', stream_name='data',
                 **properties):
        ## Build up the list of targets
        self.mode = mode
        self.targets = {}
        try:
            count = 0
            while 1:
                target = properties['target%d' % count]
                ## We can not open the target for reading at the same
                ## time as we are trying to write it - this is
                ## danegerous and may lead to file corruption.
                if mode!='w':
                    self.targets[count] = fd.open_stream(target)
                    
                count +=1
        except KeyError:
            pass

        if count==0:
            raise RuntimeError("You must set some targets of the mapping stream")

        ## Get the underlying FIFFile
        self.fd = fd
        
        ## This holds all the file offsets on the map in sorted order
        self.points = []

        ## This holds the image offsets corresponding to each file offset.
        self.mapping = {}

        ## This holds the target index for each file offset
        self.target_index = {}
        
        self.properties = properties
        self.stream_name = stream_name
        self.readptr = 0
        self.size = int(properties.get('size',0))
        ## Check if there is a map to load:
        if mode=='r':
            self.load_map()
        
    def del_point(self, file_pos):
        """ Remove the point at file_pos if it exists """
        idx = self.points.index(file_pos)
        try:
            del self.mapping[file_pos]
            del self.target_index[file_pos]
            self.points.pop(idx)
        except:
            pass

    def add_point(self, file_pos, image_pos, target_index):
        """ Adds a new point to the mapping function. Points may be
        added in any order.
        """
        bisect.insort_left(self.points, file_pos)
        ## Check to see if the new point is different than what is to
        ## be expected - this assists in compressing the mapping
        ## function because we never store points unnecessarily.
        self.mapping[file_pos] = image_pos
        self.target_index[file_pos] = target_index

    def pack(self):
        """ Rewrites the points array to represent the current map
        better
        """
        last_file_point = self.points.pop(0)
        result = [last_file_point]
        last_image_point = self.mapping[last_file_point]
        
        for point in self.points:
            ## interpolate
            interpolated = last_image_point + (point - last_file_point)
            last_file_point = point
            last_image_point = self.mapping[last_file_point]
            
            if interpolated != last_image_point:
                result.append(point)

        self.points = result

    def seek(self, offset, whence=0):
        if whence==0:
            self.readptr = offset
        elif whence==1:
            self.readptr += offset
        elif whence==2:
            self.readptr = self.size or self.points[-1]

    def interpolate(self, file_offset, direction_forward=True):
        """ Provides a tuple of (image_offset, valid length) for the
        file_offset provided. The valid length is the number of bytes
        until the next discontinuity.
        """
        try:
            file_period = int(self.properties['file_period'])
            image_period = int(self.properties['image_period'])
            period_number = file_offset / file_period
            file_offset = file_offset % file_period
        except KeyError:
            period_number = 0
            image_period = 0
            file_period = self.size
        ## We can't interpolate forward before the first point - must
        ## interpolate backwards.
        if file_offset < self.points[0]:
            direction_forward = False

        ## We can't interpolate backwards after the last point, we must
        ## interpolate forwards.
        elif file_offset > self.points[-1]:
            direction_forward = True

        if direction_forward:
            l = bisect.bisect_right(self.points, file_offset)-1
            try:
                left = self.points[l+1] - file_offset
            except:
                left = file_period - file_offset

            point = self.points[l]
            image_offset = self.mapping[point]+file_offset - point
        else:
            r = bisect.bisect_right(self.points, file_offset)

            point = self.points[r]
            
            image_offset =self.mapping[point] - (point - file_offset)
            left = point - file_offset
            
        return (image_offset + image_period * period_number, left, self.target_index[point])

    def tell(self):
        return self.readptr

    def read(self, length):
        result = ''
        ## Cant read beyond end of file
        length = min(self.size - self.readptr, length)
        
        while length>0:
            m, left, target_index = self.interpolate(self.readptr)
            ## Which target to read from?
            fd = self.targets[target_index]

            fd.seek(m,0)
            want_to_read = min(left, length)
            
            data = fd.read(want_to_read)
            if len(data)==0: break

            self.readptr += len(data)
            result += data
            length -= len(data)

        return result

    def save_map(self):
        """ Saves the map onto the fif file.
        """
        result = ''
        for x in self.points:
            result += "%s %s %s\n" % (x, self.mapping[x], self.target_index[x])

        filename = "%s/map" % self.stream_name
        self.fd.writestr(filename, result)

    def load_map(self):
        """ Opens the mapfile and loads the points from it """
        filename = "%s/map" % self.stream_name
        result =self.fd.read_member(filename)

        for line in result.splitlines():
            line = line.strip()
            if line.startswith("#"): continue
            try:
                temp = re.split("[\t ]+", line, 2)
                off = temp[0]
                image_off = temp[1]
                target_index = temp[2]

                self.add_point(int(off), int(image_off), int(target_index))
            except (ValueError,IndexError),e:
                pass

    def plot(self, title='', filename=None, type='png'):
        """ A utility to plot the mapping function """
        max_size = self.size
        p = os.popen("gnuplot", "w")
        if filename:
            p.write("set term %s\n" % type)
            p.write("set output \"%s\"\n" % filename)
        else:
            p.write("set term x11\n")
            
        p.write('pl "-" title "%s" w l, "-" title "." w p ps 5\n' % title)

        for point in self.points:
            p.write("%s %s\n" % (point, self.mapping[point]))
            
        p.write("e\n")

        for i in self.points:
            p.write("%s %s\n" % (i,self.mapping[i]))

        p.write("e\n")
        if not filename:
            p.write("pause 10\n")
            
        p.flush()

        return p

class Encrypted(FIFFD):
    """ This stream contains a number of FIF volumes within it.

    We actually return a FIFFile object which is backed by us. We are
    a transparent file like object used for filtering read/writes.
    """
    def __init__(self, mode='w', fd=None, stream_name='data', **properties):
        self.outer_fif_file = fd
        self.name = self.stream_name = stream_name
        self.mode = mode
        self.fd = fd
        self.properties = properties
        self.size = 0
        self.type = 'Encrypted'
        self.outstanding = ''
        volume_name = "%s/crypted" % stream_name

        if mode=='w':
            self.encrypted_fif_fd = self.outer_fif_file.open_stream_for_writing(volume_name)
            return
        else:
            self.encrypted_fif_fd = self.outer_fif_file.open_stream(volume_name)
            
    def write(self, data):
        print "%s: %r" % (self.encrypted_fif_fd.readptr, data)
        self.encrypted_fif_fd.write(data)

    def read(self, length=None):
        self.encrypted_fif_fd.read(length)

    def tell(self):
        return self.encrypted_fif_fd.tell()

    def flush(self):
        return self.encrypted_fif_fd.flush()

## The following are the supported segment types
types = dict(Image=Image, Map=MapDriver, Encrypted=Encrypted)
