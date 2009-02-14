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

    ## This is the zip file we are currently writing to - it must
    ## either be created by create_new_volume, or set to an existing
    ## volume with append_volume
    written_to_volume = None
    
    ## The index into the zipfiles array for the currently written
    ## file
    written_index = 0
    
    ## This is an estimate of our current size
    size = 0
    
    def __init__(self, filenames=[]):
        ## This is an index of all files in the archive set (tuples of
        ## ZipInfo and ZipFile index)
        self.file_offsets = {}
        self.Store = Store()
        self.zipfiles = []
        
        for filename in filenames:
            zf = zipfile.ZipFile(filename,
                                 mode='r', allowZip64=True)
            self.zipfiles.append(zf)
            zipindex = len(self.zipfiles) - 1
            
            for zinfo in zf.infolist():
                filename = zinfo.filename
                self.update_index(filename, zinfo, zipindex)
                
                ## Check the UUID:
                if filename=='properties':
                    props = self.parse_properties_from_string(zf.read("properties"))
                    if self.UUID:
                        assert(self.UUID == props['UUID'],
                               "File %s does not have the right UUID" % filename)
                    else:
                        self.UUID = props['UUID']

    def update_index(self, filename, zinfo, zipindex):
        """ Updates the offset index to point within the zip file we
        need to read from. This is done so we dont need to reparse the
        zip headers all the time (probably wont be needed in the C
        implementation - just that python is abit slow)
        """
        try:
            ## Update the index with the more recent zinfo
            row = self.file_offsets[filename]
            if row[4] > zinfo.date_time:
                return
        except KeyError:
            pass

        ## Work out where the compressed file should start
        offset = zinfo.header_offset + len(zinfo.FileHeader())

        self.file_offsets[filename] = ( zipindex, offset,
                                        zinfo.compress_size, zinfo.compress_type,
                                        zinfo.date_time)            

    def writestr(self, member_name, data):
        """ This method write a component to the archive. """
        if not self.written_to_volume:
            raise RuntimeError("Trying to write to archive but no archive was set")

        ## FIXME - implement digital signatures here
        self.written_to_volume.writestr(member_name, data)

        ## Make sure to update the offset cache with the new details.
        zinfo = self.written_to_volume.getinfo(member_name)
        self.update_index(member_name, zinfo, self.written_index)
        
        ## A bit extra for the headers etc
        self.size = self.written_to_volume.fp.tell()

    def read(self, filename):
        try:
            return self.Store.get(filename)
        except KeyError:
            zipindex, offset, length, compress_type, date_time = \
                      self.file_offsets[filename]

            zf = self.zipfiles[zipindex].fp
            try:
                zf.seek(offset)
                bytes = zf.read(length)
            except IOError:
                ## Try to open the file in read mode
                zf = open(zf.name,'r')
                zf.seek(offset)
                bytes = zf.read(length)
                zf.close()
                
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
        if not self.UUID:
            self.UUID = uuid.uuid4().__str__()

        ## Close off any outstanding volumes
        self.close()
        for i in range(len(self.zipfiles)):
            if self.zipfiles[i].filename == filename:
                self.zipfiles[i] = self.written_to_volume = \
                                   zipfile.ZipFile(filename, mode='a',
                                                   compression=zipfile.ZIP_DEFLATED,
                                                   allowZip64 = True)
                self.written_index = i
                return

        raise RuntimeError("Cant append to file %s - its not part of the FIF set" % filename)

    def create_new_volume(self, filename):
        """ Creates a new volume called filename. """
        if not self.UUID:
            self.UUID = uuid.uuid4().__str__()

        ## Close off any outstanding volumes
        self.close()
        self.written_to_volume = zipfile.ZipFile(filename, mode='w',
                                                 compression=zipfile.ZIP_DEFLATED,
                                                 allowZip64 = True)

        ## The new zip file automatically becomes part of the archive:
        self.zipfiles.append(self.written_to_volume)
        self.written_index = len(self.zipfiles) - 1

    def format_properties(self, properties):
        result = ''
        for k,v in properties.items():
            result+="%s=%s\n" % (k,v)

        return result

    def close(self):
        """ Finalise any created volume """
        if self.written_to_volume:
            self.written_to_volume.writestr("properties",
                                            self.format_properties(dict(
                UUID = self.UUID,
                )))

            filename = self.written_to_volume.filename
            print "Closing %s" % filename
            self.written_to_volume.close()
                
            ## Reopen the zip file in r mode:
            self.zipfiles[self.written_index] = zipfile.ZipFile(filename,
                                                                mode='r',
                                                                allowZip64 = True)
            self.written_to_volume = None
            self.size = 0

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
            self.read("%s/properties" % stream_name))

        stream = types[properties['type']]
        return stream(fd=self, stream_name=stream_name, mode='r', **properties)

    def stats(self):
        print "Store load %s chunks total %s (expired %s)" % (
            len(self.Store.seq),
            self.Store.size,
            self.Store.expired,
            )

    def __del__(self):
        self.close()

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

    def close(self):
        if self.mode == 'w':
            self.write_properties(self.fd)        
            #self.fd.close()

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

        chunk = self.fd.read(self.make_chunk_name(chunk_id))
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
        result =self.fd.read(filename)

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

## The following are the supported segment types
types = dict(Image=Image, Map=MapDriver)
