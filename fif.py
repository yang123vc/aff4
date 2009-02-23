import zipfile, struct, zlib, re
import time, binascii
import bisect, os, uuid, sys, sha

## The version of this implementation of FIF
VERSION = "FIF1.0"
CONTENT_TYPE = 'application/x-fif-file'

## Alas python zlib grabs the GIL so threads dont give much. This
## should be fixed soon: http://bugs.python.org/issue4738
## For now we dont use threads
## NUMBER_OF_THREADS = 2

class Store:
    """ A limited cache implementation """
    def __init__(self, limit=5000000):
        """ limit is the maximum size of the cache.

        When exceeded we start to expire objects. We estimate the size
        of each object by calling a len() method on it - this is ok
        for strings but might not be good for large objects.
        """
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

class properties:
    """ A class to manage the properties file.

    properties are key value pairs which go into a file named
    'properties':

    key=value

    One on each line. There can be multiple values of each key but
    key,value pairs are unique. If there are multiple values for the
    same key the order in which they appear is important and must be
    preserved, however the ordering of different keys is
    arbitrary. keys and values are seperated by '=' which may not
    appear in the key name. Keys are case insensitive, but values are
    case sensitive. Values may represent text in which case they are
    encoded in UTF8. If they represent binary data they should be
    encoded in base64.
    """
    def __init__(self, init_string=None):
        self.properties = {}
        if init_string:
            self.from_string(init_string)

    def __getitem__(self, key):
        result = self.properties[key]
        return result[0]

    def getarray(self, key):
        return self.properties[key]

    def __setitem__(self, key, value):
        try:
            if value not in self.getarray(key):
                self.properties[key].append(value)
        except (KeyError, ValueError):
            self.properties[key] = [ value ]

    def from_string(self, string):
        for line in string.splitlines():
            k,v = line.split("=",1)
            self[k] = v

    def __str__(self):
        result = ''
        for k,v in self.items():
            result+="%s=%s\n" % (k,v)

        return result

    def items(self):
        for k,v in self.properties.items():
            for value in v:
                yield (k,value)
                
    def update(self, p):
        for k,v in p.items():
            try:
                if self[k] != v:
                    self[k] = v
            except KeyError:
                self[k] = v

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __delitem__(self, key):
        try:
            del self.properties[key]
        except KeyError:
            pass
        
    def set(self, key, value):
        del self[key]
        self[key] = value
                
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
    ## This is the file object which is currently being written to.
    fp = None

    ## This is an estimate of our current size
    size = 0

    ## This is a write lock for the current FIF file
    wlock = None

    _didModify = False
    start_dir = 0

    def __init__(self, filenames=[], parent=None, autoload = True):
        """ Build a new FIFFile object.

        filenames specify a list of file names or file like object
        which will be opened as volumes. parent is another fif file
        which this file will be created as a child of (therefore we
        will share its UUID).

        autoload specifies if we should attempt to automatically load
        all volumes referenced by this volume.
        """
        ## This is an index of all files in the archive set
        if type(filenames)==str:
            filenames = [filenames]
            
        self.file_offsets = {}        
        self.Store = Store()
        self.zipfiles = []
        self.properties = properties()
        self.properties['version'] = VERSION
        if parent:
            self.properties.set('UUID', parent.properties['UUID'])
        
        ## This is used to keep track of the current write position in
        ## the file. This is because the file may be read and written
        ## at the same time. Read position is not important because
        ## its taken from the file_offsets index each time.
        self.readptr = 0
        
        ## This keeps track of the currently outstanding
        ## writers. Writers get notified with a flush() when the FIF
        ## file needs to do something, e.g. change volume. This gives
        ## them a chance to dump out critical information (e.g. key
        ## material). Note that writers are not expected to finalise
        ## their stream when we call flush() because we might switch
        ## to another volume and they can keep going, flushing
        ## typically only involves dumping enough information to be
        ## able to use the portion of the stream in this volume all by
        ## itself - in case other volumes get lost.
        self.writers = []
        
        ## These are the volumes that we currently process
        self.volumes = set()

        ## Initialise our baseclass for writing - writing is mainly
        ## handled by our base class, while reading is done directly
        ## using the file_offsets index.
        zipfile.ZipFile.__init__(self, None, mode='w',
                                 compression=zipfile.ZIP_DEFLATED,
                                 allowZip64=True)

        ## Load in any files given
        for f in filenames:
            self.merge_fif_volumes(f)

        ## Now recursively load in any other volumes:
        while autoload:
            volume = self.volume_loaded()
            ## No unresolved volumes left
            if not volume: break

            self.merge_fif_volumes(volume)

        ## Now get a UUID if needed. We could have been provided a
        ## UUID from our parent or from any of the volume we loaded -
        ## but if not we need to come up with a new one:
        try:
            self.properties['UUID']
        except KeyError:
            self.properties['UUID'] = uuid.uuid4().__str__()

    def volume_loaded(self):
        """ Returns another volume to be loaded from the current FIF
        set
        """
        try:
            ## First do the file:// volumes
            for volume in self.properties.getarray('volume'):
                if "file://" in volume and volume not in self.volumes:
                    return volume

            ## Now do everything else
            for volume in self.properties.getarray('volume'):
                if volume not in self.volumes:
                    return volume
        except KeyError:
            pass

    def resolve_url(self, fileobj):
        """ Given a url, or stream name or file like object returns a
        file like object.
        """
        ## Its already file like object
        try:
            fileobj.seek
            fileobj.read
            fileobj.write
        except AttributeError:
            try:
                ## Could be a plain filename
                fileobj = open(fileobj,'r+b')
            except IOError:
                ## it refers to an external file
                if fileobj.startswith("file://"):
                    return open(fileobj[len("file://"):],'r+b')
                else:
                    ## Maybe its a stream within this current FIF
                    ## fileset
                    fileobj = self.open_stream(fileobj)
                    
        return fileobj

    def merge_fif_volumes(self, url):
        """ Load and merge the FIF volumes specified. """
        fileobj = self.resolve_url(url)
        print "Loading %s %s" % (url, fileobj)
                
        if fileobj not in self.zipfiles:
            self.zipfiles.append(fileobj)

        ## We parse out the CD of each file and build an index
        zf = zipfile.ZipFile(fileobj,
                             mode='r', allowZip64=True)

        for zinfo in zf.infolist():
            filename = zinfo.filename
            self.update_index(fileobj, zinfo)

            ## Check the UUID:
            if filename=='properties':
                p = properties(zf.read("properties"))
                print p
                try:
                    UUID = self.properties['UUID']
                    if p['UUID'] != self.properties['UUID']:
                        raise RuntimeError("File %s does not have the right UUID" % filename)
                except KeyError:
                    self.properties['UUID'] = p['UUID']
                    
                ## Ok we add it to our volume set
                if type(fileobj)==file:
                    name = "file://%s" % os.path.basename(fileobj.name)
                else:
                    name = fileobj.name

                if name not in self.volumes:
                    self.volumes.add(name)
                    self.properties['volume'] = name
                    
                self.properties.update(p)

    def update_index(self, fileobj, zinfo):
        """ Updates the offset index to point within the zip file we
        need to read from. This is done so we dont need to reparse the
        zip headers all the time (probably wont be needed in the C
        implementation - just that python is abit slow)
        """
        try:
            ## Update the index with the more recent zinfo
            row = self.file_offsets[zinfo.filename]
            if row[4] > zinfo.date_time:
                return
        except KeyError:
            pass

        ## Work out where the compressed file should start
        offset = zinfo.header_offset + len(zinfo.FileHeader())

        self.file_offsets[zinfo.filename] = ( fileobj, offset,
                                              zinfo.compress_size, zinfo.compress_type,
                                              zinfo.date_time)            

    def open_member(self, stream_name, mode='r',
                    compression=zipfile.ZIP_STORED):
        """ This opens an archive member for random access.

        We can either open the member in read mode or write mode. In
        write mode we receive a stream directly into the archive. You
        must call close() explicitely on the returned stream - this
        will cause the fileheaders to be finalised. Note that you can
        not open a compressed member for random access, but you can
        write a compressed member using the stream interface (as an
        alternative to writestr() - this is suitable for very large
        members).
        """
        zinfo = zipfile.ZipInfo(stream_name, date_time=time.gmtime())

        if mode == 'w':
            ## check the lock:
            if self.wlock:
                raise IOError("Zipfile is currently locked for writing member %s" % self.wlock)

            self.wlock = stream_name

            ## We are about to write an unspecified length member
            zinfo.compress_size = zinfo.file_size = zinfo.CRC = 0
            zinfo.flag_bits = 0x08
            zinfo.compress_type = compression
            zinfo.header_offset = self.readptr
            
            ## Write the zinfo header like that
            self.write(zinfo.FileHeader())

            return ZipFileStream(self, 'w', self.fp, zinfo, self.fp.tell())
        else:
            ## Get the offset ranges from the cache
            fp, offset, size, type, date_time = self.file_offsets[stream_name]
            if type == zipfile.ZIP_DEFLATED:
                raise RuntimeError("Unable to open compressed archive members for random access")
            
            zinfo.file_size = size

            return ZipFileStream(self, 'r', fp, zinfo, offset)
    
    def writestr(self, member_name, data, compression=zipfile.ZIP_STORED):
        """ This method write a component to the archive. """
        if not self.fp:
            raise RuntimeError("Trying to write to archive but no archive was set - did you need to call create_new_volume() or append_volume() first?")

        ## FIXME - implement digital signatures here
        data = data.__str__()
        m = self.open_member(member_name, 'w', compression = compression)
        m.write(data)
        m.close()

    def write(self, data):
        self.fp.seek(self.readptr)
        self.fp.write(data)
        self.readptr += len(data)
        self.size = max(self.size, self.readptr)
        self._didModify = True

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
                ## We start to add members at this point:
                self.readptr = self.start_dir
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
        self.size = 0
        self.readptr = 0
        
        ## We now will be operating on this - reinitialise ourselves
        ## for writing on it (this will clear out of CD list):
        zipfile.ZipFile.__init__(self, filename, mode='w', compression=zipfile.ZIP_DEFLATED,
                                 allowZip64 = True)

    def flush(self):
        ## Tell all our outstanding writers to flush:
        for x in self.writers:
            x.flush()

        #try:
        #    self.fp.flush()
        #except AttributeError:
        #    pass
        
    def close(self):
        """ Finalise any created volume """
        if self._didModify and self.fp:
            try:
                self.properties['UUID']
            except KeyError:
                self.properties['UUID'] = uuid.uuid4().__str__()

            ##self.properties.set("current_volume", self.current_volume)
                
            self.writestr("properties", self.properties)
            self.flush()

            ## Call our base class to close us - this will dump out
            ## the CD
            self.fp.seek(self.readptr)
            zipfile.ZipFile.close(self)

    def create_stream_for_writing(self, stream_name='default',
                                  stream_type='Image',
                                  **args):
        """ This method creates a new stream for writing in the
        current FIF archive.

        We essentially instantiate the driver named by stream_type for
        writing and return it.
        """
        try:
            props = args['properties']
            del args['properties']
        except:
            props = properties()
            
        stream = types[stream_type]
        new_stream = stream(fd=self, stream_name=stream_name, mode='w',
                            properties=props, **args)
        self.writers.append( new_stream)
        return new_stream

    def open_stream(self, stream_name):
        """ Opens the specified stream from out FIF set.

        We basically instantiate the right driver and return it.
        """
        props = properties(self.read_member("%s/properties" % stream_name))

        stream = types[props['type']]
        return stream(fd=self, stream_name=stream_name, mode='r',
                      properties=props)

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
    def write_properties(self):
        """ This returns a formatted properties string. Properties are
        related to the stream. target is a zipfile.
        """
        ## This will change as we go along so we only want one of
        ## these:
        self.properties.set('size', self.size)
        self.properties['type'] = self.type
        self.properties['name'] = self.stream_name
        
        ## Make the properties file
        filename = "%s/properties" % self.stream_name
        self.fd.writestr(filename, self.properties)

    def seek(self, pos, whence=0):
        #print "Seeking %s, %s %s" % (pos, whence, self.size)
        if whence==0:
            self.readptr = pos
        elif whence==1:
            self.readptr += pos
        elif whence==2:
            self.readptr = self.size + pos

    def tell(self):
        return self.readptr

    def flush(self):
        """ This gets called when we need to ensure that volatile
        stuff is on disk. For example, when changing volumes.
        """
        ## By default we write multiple copies of the properties
        ## file. We do this so that each volume is completely stand
        ## alone.
        if self.mode == 'w':
            self.write_properties()
        
    def close(self):
        ## Remove ourselves from the parent's outstanding writers list
        self.flush()
        try:
            idx = self.fd.writers.index(self)
            self.fd.writers.pop(idx)
        except IndexError:
            pass

class ZipFileStream(FIFFD):
    """ This is a file like object which represents a zip file member """
    def __init__(self, parent,mode, fp, zinfo, file_offset):
        ## This is the FIFFile we belong to
        self.fp = fp
        self.mode = mode
        self.readptr = 0
        self.size = zinfo.file_size
        self.zinfo = zinfo
        self.file_offset = file_offset
        self.parent = parent
        if zinfo.compress_type == zipfile.ZIP_DEFLATED:
            self.compr = zlib.compressobj(9,
                                          zlib.DEFLATED, -15)
        else:
            self.compr = None

    def read(self, length=None):
        if self.compr: raise RuntimeError("Can not read compressed member opened for writing")
        if length==None: length = sys.maxint
        
        length = min(length, self.size - self.readptr)

        self.fp.seek(self.readptr + self.file_offset)
        data = self.fp.read(length)
        self.readptr += len(data)
        return data

    def seek(self, off, whence=0):
        if self.compr:
            raise RuntimeError("Unable to seek in compressed streams")
        
        FIFFD.seek(self,off, whence)

    def write(self, data):
        if self.mode != 'w': raise RuntimeError("Stream not opened for writing")

        ## Update the zinfo:
        self.zinfo.CRC = binascii.crc32(data, self.zinfo.CRC)
        self.zinfo.file_size += len(data)

        if self.compr:
            data = self.compr.compress(data)

        self.zinfo.compress_size += len(data)    
        self.parent.write(data)
                
    def close(self):
        if self.mode == 'r': return

        ## Flush decompressor
        if self.compr:
            data = self.compr.flush()
            self.zinfo.compress_size += len(data)    
            self.parent.write(data)
            chunk_size = self.zinfo.compress_size
        else:
            chunk_size = self.zinfo.file_size
            
        ## Write the data descriptor
        self.parent.write(struct.pack("<lLL", self.zinfo.CRC,
                                      self.size,
                                      self.size,))

        ## Add the new zinfo to the CD
        self.parent.filelist.append(self.zinfo)
        self.parent.NameToInfo[self.zinfo.filename] = self.zinfo

        ## Invalidate cache
        self.parent.Store.expire(self.zinfo.filename)

        ## Add to the read cache
        self.parent.file_offsets[self.zinfo.filename] = (
            self.parent.fp,
            ## Offset to the start of the file (after the header)
            self.file_offset,
            chunk_size,
            self.zinfo.compress_type,
            self.zinfo.date_time )
        
        ## Remove the write lock from the parent
        self.parent.wlock = False

    def tell(self):
        return self.readptr

class Image(FIFFD):
    """ A stream writer for creating a new FIF archive """
    type = "Image"
    size = 0
    
    def __init__(self, mode='w', stream_name='data',
                 attributes=None, fd=None,
                 properties = None, **args):
        properties.update(args)
        self.fd = fd
        self.mode = mode
        self.name = self.stream_name = stream_name
        self.chunk_id = 0
        self.chunksize = int(properties.get('chunk_size', 32*1024))
        self.size = int(properties.get('size',0))
        self.readptr = 0
        self.Store = Store()
        self.stream_re = re.compile(r"%s/(\d+)+.dd" % self.stream_name)
        self.outstanding = ''

        ## The following are mandatory properties:
        self.properties = properties

    def read(self, length=None):
        #print "length ",self.readptr, length, 
        if length==None: length=sys.maxint
        result = ''
        length = min(self.size - self.readptr, length)
        while length>0:
            data= self.partial_read(length)
            if len(data)==0: break
            
            length -= len(data)
            result += data
            self.readptr += len(data)

        #print len(result), self.readptr, "%r" % result[-50:], self.size
        return result
    
    def partial_read(self, length):
        ## which chunk is it?
        chunk_id = self.readptr / self.chunksize
        chunk_offset = self.readptr % self.chunksize
        available_to_read = min(self.chunksize - chunk_offset, length)

        chunk = self.read_chunk(chunk_id)
        #print "chunk_id", chunk_id, len(chunk), chunk_offset, 
        return chunk[chunk_offset:chunk_offset+available_to_read]

    def read_chunk(self, chunk_id):
        return self.fd.read_member(self.make_chunk_name(chunk_id))

    def write_properties(self):
        self.properties.set('count', self.chunk_id)
        FIFFD.write_properties(self)
        
    def make_chunk_name(self, chunk_id):
        return "%s/%08d.dd" % (self.stream_name, chunk_id)

    def write(self, data):
        self.readptr += len(data)
        self.size = max(self.size, self.readptr)
        #print "%s Writing %s, %s %r" % (self, self.readptr, len(data), data[-50:])
        data = self.outstanding + data
        while len(data)>self.chunksize:
            chunk = data[:self.chunksize]
            self.write_chunk(chunk)
            data = data[self.chunksize:]

        self.outstanding = data
        #print "Outstanding %s" % len(self.outstanding)
        
    def write_chunk(self, data):
        """ Adds the chunk to the archive """
        name = self.make_chunk_name(self.chunk_id)
        self.fd.writestr(name, data, zipfile.ZIP_DEFLATED)
        self.chunk_id += 1

    def close(self):
        """ Finalise the archive """
        #print "Closing archive with %s %s %s" % (len(self.outstanding),self.size, self.readptr)
        ## Write the last chunk
        if len(self.outstanding)>0:
            self.write_chunk(self.outstanding)
            
        FIFFD.close(self)

class Overlay(Image):
    """ The overlay stream allows for external chunk references so we
    can just piggy back on top of files like EWF.
    """
    type = 'Overlay'
    def __init__(self, mode='w', *args, **kwargs):
        self.chunks = {}
        self.filenames = {}
        self.inverted_filenames = {}
        self.overlay_count = 0
        Image.__init__(self, mode=mode, *args, **kwargs)
        if mode=='r':
            self.open_overlay()

    def open_overlay(self):
        self.targets = []
        for t in self.properties.getarray('target'):
            self.targets.append(self.fd.resolve_url(t))
        
        for segment in self.properties.getarray('overlay'):
            data = self.fd.read_member("%s/%s" % (self.stream_name, segment))
            for line in data.splitlines():
                id, offset, size, compression, filename_id = line.split(",",5)
                id = int(id)
                offset = long(offset)
                size = long(size)
                filename_id = int(filename_id)
                compression = int(compression)
                self.chunks[id] = (offset, size, compression, filename_id)

    def set_chunk(self, chunk_id, offset, size, compression, filename):
        try:
            filename_id = self.filenames[filename]
        except KeyError:
            ## Set a new filename
            filename_id = len(self.filenames.keys())
            self.filenames[filename] = filename_id
            self.inverted_filenames[filename_id] = filename

        self.chunks[chunk_id] = (offset, size, compression, filename_id)
        self.chunk_id = max(self.chunk_id, chunk_id)

    def close(self):
        ## We add the attributes
        for i in range(len(self.inverted_filenames.keys())):
            self.properties['target'] = 'file://%s' % self.inverted_filenames[i].strip()

        name = "overlay.%02d" % (self.overlay_count)
        self.overlay_count += 1
        fd = self.fd.open_member("%s/%s" % (self.stream_name, name),
                                 mode="w",
                                 compression=zipfile.ZIP_DEFLATED)

        for i in range(self.chunk_id+1):
            try:
                row = self.chunks[i]
                fd.write("%s,%s\n" % (i,",".join(["%d" % x for x in row])))
            except Exception,e:
                print e
                pass

        fd.close()
        self.properties['overlay'] = name
        Image.close(self)
        
    def read_chunk(self, chunk_id):
        (offset, size, compression, filename_id) = self.chunks[chunk_id]

        target = self.targets[filename_id]
        target.seek(offset)
        data = target.read(size)
        
        if compression:
            data = zlib.decompress(data)
            ## Decompress it now:
            #dc = zlib.decompressobj(-15)
            #data = dc.decompress(data)
            # need to feed in unused pad byte so that zlib won't choke
            #ex = dc.decompress('Z') + dc.flush()
            #if ex:
            #    data = data + ex
                
        return data
    
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
                 properties=None, **args):
        properties.update(args)
        ## Build up the list of targets
        self.mode = mode
        self.targets = {}
        try:
            count = 0
            for target in properties.getarray('target'):
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

    def read(self, length=None):
        if length==None: length=sys.maxint
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
        self.fd.writestr(filename, result, zipfile.ZIP_DEFLATED)

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


class NULLScheme:
    """ This is the NULL encryption Scheme """
    def __init__(self, fiffile, stream_name, properties):
        self.master_key = None

    def encrypt_block(self, count, block):
        return block

    def decrypt_block(self, count, block):
        return block

crypto_schemes = {"null": NULLScheme,}

try:
    import Crypto.Hash.MD5
    import Crypto.Hash.SHA

    import Crypto.Cipher.AES
    import Crypto
    import Crypto.Util.randpool

    ## This is a crypto pool for key material
    POOL = Crypto.Util.randpool.RandomPool()

    class AES_SHA_PSK(NULLScheme):
        hash_class = Crypto.Hash.SHA.new
        crypt_class = Crypto.Cipher.AES.new
        mode = Crypto.Cipher.AES.MODE_CBC
        ## This is the cipher's block size
        block_size = 16
        key_size = 16
        pad = 0
        
        def __init__(self, fiffile, stream_name, properties):
            ## We derive the master key from PSK here
            self.properties = properties
            self.get_master_key()

        def get_master_key(self):
            try:
                salt = self.properties['salt'].decode("base64")
            except KeyError:
                salt = POOL.get_bytes(8)
                self.properties['salt'] = salt.encode("base64").strip()
                
            try:
                PSK = os.environ['FIF_PSK']
                print "Read PSK from environment"
            except KeyError:
                try:
                    PSK = self.properties['PSK']
                    ## Clear the PSK from the properties so it doesnt
                    ## get written to file
                    del self.properties['PSK']
                except KeyError:
                    PSK = raw_input("Type in a password:")

            self.master_key = self.hash_class(PSK+salt).digest()[:self.key_size]

        def encrypt_block(self, count, block):
            self.pad = len(block) % self.block_size
            if self.pad:
                #print "Padding from %s to %s" % (len(block),self.pad)
                block += "\xFF" * (self.block_size -self.pad)
                
            IV = self.hash_class(struct.pack("<L",count) + \
                                 self.master_key).digest()[:self.key_size]

            aes = self.crypt_class(self.master_key, self.mode,
                                   IV)

            return aes.encrypt(block)

        def decrypt_block(self, count, block):
            IV = self.hash_class(struct.pack("<L",count) + \
                                 self.master_key).digest()[:self.key_size]

            aes = self.crypt_class(self.master_key, self.mode,
                                   IV)

            return aes.decrypt(block)

    crypto_schemes['aes-sha-psk'] = AES_SHA_PSK

except ImportError:
    Crypto = None

class Encrypted(Image):
    type = "Encrypted"
    fiffile = None

    def __init__(self, stream_name='crypt', properties = None, blocksize=4096,
                 **args):
        Image.__init__(self, stream_name=stream_name,
                       properties = properties, **args)
        print properties
        try:
            scheme = properties['scheme']
        except KeyError:
            scheme = 'null'
            print "No scheme specified, defaulting to %s" % scheme
            properties['scheme'] = scheme

        try:
            self.crypto = crypto_schemes[scheme]
        except KeyError:
            raise RuntimeError("Crypto scheme %s not implemented" % scheme)
        
        ## Default chunksize for encrypted is 16MB
        self.blocksize = int(blocksize)
        self.crypto = self.crypto(self.fd, self.stream_name, self.properties)
        self.Store = Store()
            
    def write_chunk(self, data):
        #print "Writing encrypted chunk %s %s" % (self.chunk_id, self.size)
        name = self.make_chunk_name(self.chunk_id)
        data = self.crypto.encrypt_block(self.chunk_id, data)
        self.fd.writestr(name, data, zipfile.ZIP_STORED)
        self.chunk_id += 1

    def read_chunk(self, chunk_id):
        name = self.make_chunk_name(chunk_id)
        try:
            data = self.Store.get(name)
        except KeyError:
            data = self.fd.read_member(name)
            data = self.crypto.decrypt_block(chunk_id, data)
            self.Store.put(name, data)
            
        return data

    def create_new_volume(self):
        """ This is a convenience method for creating a fif file
        inside the encrypted stream.
        """
        ## We contain a fif file
        self.properties['content-type'] = CONTENT_TYPE
        self.fd.properties['volume'] = self.stream_name
        
        self.fiffile = FIFFile(parent = self.fd)
        self.fiffile.create_new_volume(self)

        return self.fiffile

    def close(self):
        if self.properties.get('content-type') == CONTENT_TYPE:
        ## Make sure that our container is closed
            self.fiffile.close()

        Image.close(self)

## The following are the supported segment types
types = dict(Image=Image, Map=MapDriver, Encrypted=Encrypted,
             Overlay=Overlay)
