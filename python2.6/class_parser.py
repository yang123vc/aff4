import sys, os, re, pdb, StringIO

DEBUG = 0

def log(msg):
    if DEBUG>0:
        sys.stderr.write(msg+"\n")

def escape_for_string(string):
    result = string
    result = result.encode("string-escape")
    result = result.replace('"',r'\"')

    return result

class Module:
    def __init__(self, name):
        self.name = name
        self.constants = []
        self.classes = {}
        self.headers = "#include <Python.h>\n"
        self.files = []

    def initialization(self):
        result = """
talloc_set_log_fn((void *)printf);
AFF4_Init();

"""
        for cls in self.classes.values():
            if cls.is_active():
                result += cls.initialise()

        return result

    def add_constant(self, constant, type="numeric"):
        """ This will be called to add #define constant macros """
        self.constants.append((constant, type))

    def add_class(self, cls, handler):
        self.classes[cls.class_name] = cls

        ## Make a wrapper in the type dispatcher so we can handle
        ## passing this class from/to python
        type_dispatcher[cls.class_name] = handler

    def private_functions(self):
        """ Emits hard coded private functions for doing various things """
        return """
/* The following is a static array mapping CLASS() pointers to their
python wrappers. This is used to allow the correct wrapper to be
chosen depending on the object type found - regardless of the
prototype.

This is basically a safer way for us to cast the correct python type
depending on context rather than assuming a type based on the .h
definition. For example consider the function

AFFObject Resolver.open(uri, mode)

The .h file implies that an AFFObject object is returned, but this is
not true as most of the time an object of a derived class will be
returned. In C we cast the returned value to the correct type. In the
python wrapper we just instantiate the correct python object wrapper
at runtime depending on the actual returned type. We use this lookup
table to do so.
*/
static int TOTAL_CLASSES=0;

static struct python_wrapper_map_t {
       Object class_ref;
       PyTypeObject *python_type;
} python_wrappers[%s];

/** This is a generic wrapper type */
typedef struct {
  PyObject_HEAD
  void *base;
  void *ctx;
} Gen_wrapper;

/* Create the relevant wrapper from the item based on the lookup
table.
*/
Gen_wrapper *new_class_wrapper(Object item) {
   int i;
   Gen_wrapper *result;
   Object cls;

   // Return None for a NULL pointer
   if(!item) {
     Py_INCREF(Py_None);
     return (Gen_wrapper *)Py_None;
   };

   // Search for subclasses
   for(cls=(Object)item->__class__; cls != cls->__super__; cls=cls->__super__) {
     for(i=0; i<TOTAL_CLASSES; i++) {
       if(python_wrappers[i].class_ref == cls) {
         PyErr_Clear();

         result = (Gen_wrapper *)_PyObject_New(python_wrappers[i].python_type);
         result->ctx = talloc_asprintf(NULL, "new_class_wrapper %%s@%%p", NAMEOF(item), item);
         result->base = (void *)item;

         return result;
       };
     };
   };

  PyErr_Format(PyExc_RuntimeError, "Unable to find a wrapper for object %%s", NAMEOF(item));
  return NULL;
};

static PyObject *resolve_exception(char **error_buff) {
  enum _error_type *type = aff4_get_current_error(error_buff);
  switch(*type) {
case EProgrammingError:
    return PyExc_SystemError;
case EKeyError:
    return PyExc_KeyError;
case ERuntimeError:
    return PyExc_RuntimeError;
case EWarning:
    return PyExc_AssertionError;
default:
    return PyExc_RuntimeError;
};
};

static int type_check(PyObject *obj, PyTypeObject *type) {
   PyTypeObject *tmp;

   // Recurse through the inheritance tree and check if the types are expected
   if(obj)
     for(tmp = obj->ob_type; tmp && tmp != &PyBaseObject_Type; tmp = tmp->tp_base) {
       if(tmp == type) return 1;
     };

  return 0;
};

""" % (len(self.classes)+1)

    def initialise_class(self, class_name, out, done = None):
        if done and class_name in done: return

        done.add(class_name)

        cls = self.classes[class_name]
        """ Write out class initialisation code into the main init function. """
        if cls.is_active():
            base_class = self.classes.get(cls.base_class_name)

            if base_class and base_class.is_active():
                ## We have a base class - ensure it gets written out
                ## first:
                self.initialise_class(cls.base_class_name, out, done)

                ## Now assign ourselves as derived from them
                out.write(" %s_Type.tp_base = &%s_Type;" % (
                        cls.class_name, cls.base_class_name))

            out.write("""
 %(name)s_Type.tp_new = PyType_GenericNew;
 if (PyType_Ready(&%(name)s_Type) < 0)
     return;

 Py_INCREF((PyObject *)&%(name)s_Type);
 PyModule_AddObject(m, "%(name)s", (PyObject *)&%(name)s_Type);
""" % {'name': cls.class_name})

    def write(self, out):
        out.write("""
/**********************************************************************
     Autogenerated module %s

This module was autogenerated from the following files:
""" % self.name)
        for file in self.files:
            out.write("%s\n" % file)

        out.write("\nThis module implements the following classes:\n")
        classes = [ c.class_name for c in self.classes.values() if c.is_active() ]
        classes.sort()
        for c in classes:
            doc = self.classes[c].docstring
            try:
                doc = doc.splitlines()[0]
            except: pass

            out.write("%s - %s\n" % (c, doc.strip()))

        out.write("""***********************************************************************/
""")
        out.write(self.headers)
        out.write(self.private_functions())

        for cls in self.classes.values():
            if cls.is_active():
                cls.struct(out)
                cls.prototypes(out)

        out.write("/*****************************************************\n             Implementation\n******************************************************/\n\n")
        for cls in self.classes.values():
            if cls.is_active():
                cls.PyMethodDef(out)
                cls.code(out)
                cls.PyTypeObject(out)

        ## Write the module initializer
        out.write("""
static PyMethodDef %(module)s_methods[] = {
     {NULL}  /* Sentinel */
};

PyMODINIT_FUNC init%(module)s(void) {
   /* Make sure threads are enabled */
   PyEval_InitThreads();

   /* create module */
   PyObject *m = Py_InitModule3("%(module)s", %(module)s_methods,
                                   "%(module)s module.");
   PyObject *d = PyModule_GetDict(m);
   PyObject *tmp;
""" % {'module': self.name})

        ## The trick is to initialise the classes in order of their
        ## inheritance. The following code will order initializations
        ## according to their inheritance tree
        done = set()
        for class_name in self.classes.keys():
            self.initialise_class(class_name, out, done)

        ## Add the constants in here
        for constant, type in self.constants:
            if type == 'numeric':
                out.write(""" tmp = PyLong_FromUnsignedLongLong(%s); \n""" % constant)
            elif type == 'string':
                out.write(" tmp = PyString_FromString(%s); \n" % constant)

            out.write("""
 PyDict_SetItemString(d, "%s", tmp);
 Py_DECREF(tmp);\n""" % (constant))

        out.write(self.initialization())
        out.write("}\n\n")

class Type:
    interface = None
    buidstr = 'O'
    sense = 'IN'
    error_value = "return 0;"

    def __init__(self, name, type):
        self.name = name
        self.type = type
        self.attributes = set()

    def comment(self):
        return "%s %s " % (self.type, self.name)

    def python_name(self):
        return self.name

    def returned_python_definition(self, *arg, **kw):
        return self.definition(*arg, **kw)

    def definition(self, default=None, **kw):
        if default:
            return "%s %s=%s;\n" % (self.type, self.name, default)
        else:
            return "%s %s;\n" % (self.type, self.name)

    def byref(self):
        return "&%s" % self.name

    def call_arg(self):
        return self.name

    def pre_call(self, method):
        return ''

    def assign(self, call, method, target=None):
        return "Py_BEGIN_ALLOW_THREADS\n%s = %s;\nPy_END_ALLOW_THREADS\n" % (target or self.name, call)

    def post_call(self, method):
        if "DESTRUCTOR" in self.attributes:
            return "talloc_free(self->ctx); self->base = NULL;\n"

        return ''

    def from_python_object(self, source, destination, method, **kw):
        return ''

    def return_value(self, value):
        return "return %s;" % value


class String(Type):
    interface = 'string'
    buidstr = 's'
    error_value = "return NULL;"

    def __init__(self, name, type):
        Type.__init__(self, name, type)
        self.length = "strlen(%s)" % name

    def byref(self):
        return "&%s" % self.name

    def to_python_object(self, name=None, result='py_result',**kw):
        name = name or self.name

        result = """PyErr_Clear();
   if(!%s) goto error;
   %s = PyString_FromStringAndSize((char *)%s, %s);\nif(!%s) goto error;
""" % (name, result, name, self.length, result)
        if "BORROWED" not in self.attributes and 'BORROWED' not in kw:
            result += "talloc_unlink(NULL, %s);\n" % name

        return result

    def from_python_object(self, source, destination, method, context='NULL'):
        method.error_set = True
        return """
{
  char *buff; Py_ssize_t length;

  PyErr_Clear();
  if(-1==PyString_AsStringAndSize(%(source)s, &buff, &length))
     goto error;

  %(destination)s = talloc_size(%(context)s, length + 1);
  memcpy(%(destination)s, buff, length);
  %(destination)s[length]=0;
};
""" % dict(source = source, destination = destination, context =context)

class ZString(String):
    interface = 'null_terminated_string'

class BorrowedString(String):
    def to_python_object(self, name=None, result='py_result', **kw):
        name = name or self.name
        return "PyErr_Clear();\n" +\
            "%s = PyString_FromStringAndSize((char *)%(name)s, %(length)s);\n" % dict(
            name=name, length=self.length, result=result)

class Char_and_Length(Type):
    interface = 'char_and_length'
    buidstr = 's#'
    error_value = "return NULL;"

    def __init__(self, data, data_type, length, length_type):
        Type.__init__(self, data, data_type)

        self.name = data
        self.data_type=data_type
        self.length = length
        self.length_type = length_type

    def comment(self):
        return "%s %s, %s %s" % (self.data_type, self.name,
                                 self.length_type, self.length)

    def definition(self, default = '""', **kw):
        return "char *%s=%s; Py_ssize_t %s=strlen(%s);\n" % (
            self.name, default,
            self.length, default)

    def byref(self):
        return "&%s, &%s" % (self.name, self.length)

    def call_arg(self):
        return "(%s)%s, (%s)%s" % (self.data_type, self.name, self.length_type,
                                   self.length)

    def to_python_object(self, name=None, result='py_result', **kw):
        return "PyErr_Clear();\n"\
            "%s = PyString_FromStringAndSize(%s, %s);\nif(!%s) goto error;" % (
            result, self.name, self.length, result);

class Integer(Type):
    interface = 'integer'
    buidstr = 'K'

    def __init__(self, name,type):
        Type.__init__(self,name,type)
        self.type = 'uint64_t '
        self.original_type = type

    def definition(self, default = 0, **kw):
        return Type.definition(self, default)

    def to_python_object(self, name=None, result='py_result', **kw):
        name = name or self.name
        return "PyErr_Clear();\n%s = PyLong_FromLongLong(%s);\n" % (result, name)

    def from_python_object(self, source, destination, method, **kw):
        return "PyErr_Clear();\n"\
            "%(destination)s = PyLong_AsUnsignedLong(%(source)s);\n" % dict(
            source = source, destination= destination)

    def comment(self):
        return "%s %s " % (self.original_type, self.name)

class Integer32(Integer):
    buidstr = 'I'

    def __init__(self, name,type):
        Type.__init__(self,name,type)
        self.type = 'unsigned int '
        self.original_type = type

    def to_python_object(self, name=None, result='py_result', **kw):
        name = name or self.name
        return "PyErr_Clear();\n%s = PyLong_FromLong(%s);\n" % (result, name)

class Integer64(Integer):
    buidstr = 'K'
    type = 'unsigned int'

class Char(Integer):
    buidstr = "s"
    interface = 'small_integer'

    def to_python_object(self, name = None, result = 'py_result', **kw):
        ## We really want to return a string here
        return """{ char *str_%(name)s = &%(name)s;
    PyErr_Clear();
    %(result)s = PyString_FromStringAndSize(str_%(name)s, 1);
if(!%(result)s) goto error;
};
""" % dict(result=result, name = name or self.name)

    def definition(self, default = '"\\x0"', **kw):
        ## Shut up unused warnings
        return "char %s __attribute__((unused))=0;\nchar *str_%s __attribute__((unused)) = %s;\n" % (
            self.name,self.name, default)

    def byref(self):
        return "&str_%s" % self.name

    def pre_call(self, method):
        method.error_set = True
        return """
if(strlen(str_%(name)s)!=1) {
  PyErr_Format(PyExc_RuntimeError,
          "You must only provide a single character for arg %(name)r");
  goto error;
};

%(name)s = str_%(name)s[0];
""" % dict(name = self.name)

class StringOut(String):
    sense = 'OUT'

class IntegerOut(Integer):
    sense = 'OUT_DONE'
    buidstr = ''

    def python_name(self):
        return None

    def byref(self):
        return ''

    def call_arg(self):
        return "&%s" % self.name

class Integer32Out(Integer32):
    sense = 'OUT_DONE'
    buidstr = ''

    def python_name(self):
        return None

    def byref(self):
        return ''

    def call_arg(self):
        return "&%s" % self.name

class Char_and_Length_OUT(Char_and_Length):
    sense = 'OUT_DONE'
    buidstr = 'l'

    def definition(self, default = 0, **kw):
        return "char *%s=NULL; Py_ssize_t %s=%s;\n" % (
            self.name,
            self.length, default) + "PyObject *tmp_%s;\n" % self.name

    def python_name(self):
        return self.length

    def byref(self):
        return "&%s" % self.length

    def pre_call(self, method):
        return """PyErr_Clear();
tmp_%s = PyString_FromStringAndSize(NULL, %s);
if(!tmp_%s) goto error;
PyString_AsStringAndSize(tmp_%s, &%s, (Py_ssize_t *)&%s);
""" % (self.name, self.length, self.name, self.name, self.name, self.length)

    def to_python_object(self, name=None, result='py_result', **kw):
        name = name or self.name

        if 'results' in kw:
            kw['results'].pop(0)

        return """ _PyString_Resize(&tmp_%s, func_return); \n%s = tmp_%s;\n""" % (
            name, result, name)

class TDB_DATA_P(Char_and_Length_OUT):
    bare_type = "TDB_DATA"

    def __init__(self, name, type):
        Type.__init__(self, name, type)

    def definition(self, default=None, **kw):
        return Type.definition(self)

    def byref(self):
        return "%s.dptr, &%s.dsize" % (self.name, self.name)

    def pre_call(self, method):
        return ''

    def call_arg(self):
        return Type.call_arg(self)

    def to_python_object(self, name=None,result='py_result', **kw):
        name = name or self.name
        return "PyErr_Clear();"\
            "%s = PyString_FromStringAndSize((char *)%s->dptr, %s->dsize);"\
            "\ntalloc_free(%s);" % (result,
                                    name, name, name)

    def from_python_object(self, source, destination, method, **kw):
        method.error_set = True
        return """
%(destination)s = talloc(self, %(bare_type)s);
{ Py_ssize_t tmp; char *buf;

  PyErr_Clear();
  if(-1==PyString_AsStringAndSize(%(source)s, &buf, &tmp)) {
  goto error;
};

  // Take a copy of the python string
  %(destination)s->dptr = talloc_memdup(%(destination)s, buf, tmp);
  %(destination)s->dsize = tmp;
}
// We no longer need the python object
Py_DECREF(%(source)s);
""" % dict(source = source, destination = destination, 
           bare_type = self.bare_type)

class TDB_DATA(TDB_DATA_P):
    def to_python_object(self, name = None, result='py_result', **kw):
        name = name or self.name

        return "PyErr_Clear();\n"\
            "%s = PyString_FromStringAndSize((char *)%s.dptr, %s.dsize);\n" % (
            result,
            name, name)

class Void(Type):
    buidstr = ''
    error_value = "return;"

    def __init__(self, *args):
        Type.__init__(self, None, 'void *')

    def definition(self, default = None, **kw):
        return ''

    def to_python_object(self, name=None, result = 'py_result', **kw):
        return "Py_INCREF(Py_None); py_result = Py_None;\n"

    def call_arg(self):
        return "NULL"

    def byref(self):
        return None

    def assign(self, call, method, target=None):
        ## We dont assign the result to anything
        return "Py_BEGIN_ALLOW_THREADS\n%s;\nPy_END_ALLOW_THREADS\n" % call

    def return_value(self, value):
        return "return;"

class Wrapper(Type):
    """ This class represents a wrapped C type """
    sense = 'IN'
    error_value = "return NULL;"

    def from_python_object(self, source, destination, method, **kw):
        return """
/* First check that the returned value is in fact a Wrapper */
if(!type_check(%(source)s, &%(type)s_Type)) {
  PyErr_Format(PyExc_RuntimeError, "function must return an %(type)s instance");
  goto error;
};

%(destination)s = ((Gen_wrapper *)%(source)s)->base;
""" % dict(source = source, destination = destination, type = self.type)

    def to_python_object(self, **kw):
        return ''

    def returned_python_definition(self, default = 'NULL', sense='in', **kw):
        return "%s %s;\n" % (self.type, self.name)

    def definition(self, default = 'NULL', sense='in', **kw):
        result = "Gen_wrapper *%s __attribute__((unused)) = %s;\n" % (self.name, default)
        if sense == 'in' and not 'OUT' in self.attributes:
            result += " %s __attribute__((unused)) call_%s;\n" % (self.type, self.name)

        return result

    def call_arg(self):
        return "call_%s" % self.name

    def pre_call(self, method):
        if 'OUT' in self.attributes or self.sense == 'OUT':
            return ''

        return """
if(!%(name)s || (PyObject *)%(name)s==Py_None) {
   call_%(name)s = NULL;
} else if(!type_check((PyObject *)%(name)s,&%(type)s_Type)) {
     PyErr_Format(PyExc_RuntimeError, "%(name)s must be derived from type %(type)s");
     goto error;
} else {
   call_%(name)s = %(name)s->base;
};\n""" % self.__dict__

    def assign(self, call, method, target=None):
        method.error_set = True;
        args = dict(name = target or self.name, call = call, type = self.type)

        result = """{
       Object returned_object;

       ClearError();

       Py_BEGIN_ALLOW_THREADS
       returned_object = (Object)%(call)s;
       Py_END_ALLOW_THREADS

       if(!CheckError(EZero)) {
         char *buffer;
         PyObject *exception = resolve_exception(&buffer);

         PyErr_Format(exception,
                    "%%s", buffer);
         ClearError();
         goto error;

       // A NULL return without errors means we return None
       } else if(!returned_object) {
         %(name)s = (Gen_wrapper *)Py_None;
         Py_INCREF(Py_None);
       } else {
         //printf("%%s: Wrapping %%s@%%p\\n", __FUNCTION__, NAMEOF(returned_object), returned_object);
         %(name)s = new_class_wrapper(returned_object);
         if(!%(name)s) goto error;
""" % args

        if "BORROWED" in self.attributes:
            result += "           talloc_reference(%(name)s->ctx, %(name)s->base);\n" % args

        result += """       };
    }
"""
        return result

    def to_python_object(self, name=None, result = 'py_result', sense='in', **kw):
        name = name or self.name
        args = dict(result=result,
                    name = name)

        if sense=='proxied':
            return "%(result)s = (PyObject *)new_class_wrapper((Object)%(name)s);\n" % args

        return "%(result)s = (PyObject *)%(name)s;\n" % args

class PointerWrapper(Wrapper):
    """ A pointer to a wrapped class """
    def __init__(self, name, type):
        type = type.split()[0]
        Wrapper.__init__(self,name, type)

    def definition(self, default = 'NULL', sense='in', **kw):
        result = "Gen_wrapper *%s = %s;" % (self.name, default)
        if sense == 'in' and not 'OUT' in self.attributes:
            result += " %s *call_%s;\n" % (self.type, self.name)

        return result

    def pre_call(self, method):
        if 'OUT' in self.attributes or self.sense == 'OUT':
            return ''

        return """
if(!%(name)s || (PyObject *)%(name)s==Py_None) {
   call_%(name)s = NULL;
} else if(!type_check((PyObject *)%(name)s,&%(type)s_Type)) {
     PyErr_Format(PyExc_RuntimeError, "%(name)s must be derived from type %(type)s");
     goto error;
} else {
   call_%(name)s = (%(type)s *)&%(name)s->base;
};\n""" % self.__dict__

class StructWrapper(Wrapper):
    """ A wrapper for struct classes """
    def assign(self, call, method, target = None):
        args = dict(name = target or self.name, call = call, type = self.type)
        result = """
PyErr_Clear();
%(name)s = (Gen_wrapper *)PyObject_New(py%(type)s, &%(type)s_Type);
%(name)s->ctx = talloc_size(NULL, 1);
%(name)s->base = %(call)s;
""" % args

        if "BORROWED" in self.attributes:
            result += "talloc_reference(%(name)s->ctx, %(name)s->base);\n" % args
        else:
            result += "talloc_steal(%(name)s->ctx, %(name)s->base);\n" % args

        return result

    def byref(self):
        return "&%s" % self.name

    def definition(self, default = 'NULL', sense='in', **kw):
        result = "Gen_wrapper *%s = %s;" % (self.name, default)
        if sense == 'in' and not 'OUT' in self.attributes:
            result += " %s *call_%s;\n" % (self.type, self.name)

        return result;

class PointerStructWrapper(StructWrapper):
    def __init__(self, name, type):
        type = type.split()[0]
        Wrapper.__init__(self,name, type)

class Timeval(Type):
    """ handle struct timeval values """
    interface = 'numeric'
    buidstr = 'f'

    def definition(self, default = None, **kw):
        return "float %(name)s_flt; struct timeval %(name)s;\n" % self.__dict__

    def byref(self):
        return "&%s_flt" % self.name

    def pre_call(self, method):
        return "%(name)s.tv_sec = (int)%(name)s_flt; %(name)s.tv_usec = (%(name)s_flt - %(name)s.tv_sec) * 1e6;\n" % self.__dict__

    def to_python_object(self, name=None, result = 'py_result', **kw):
        name = name or self.name
        return """%(name)s_flt = (double)(%(name)s.tv_sec) + %(name)s.tv_usec;
%(result)s = PyFloat_FromDouble(%(name)s_flt);
""" % dict(name = name, result=result)

class PyObject(Type):
    """ Accept an opaque python object """
    interface = 'opaque'
    buidstr = 'O'
    def definition(self, default = 'NULL', **kw):
        self.default = default
        return 'PyObject *%(name)s = %(default)s;\n' % self.__dict__

    def byref(self):
        return "&%s" % self.name

type_dispatcher = {
    "IN char *": String,
    "IN unsigned char *": String,
    "unsigned char *": String,
    "char *": String,
    "ZString": ZString,

    "OUT char *": StringOut,
    "OUT unsigned char *": StringOut,
    "unsigned int": Integer,
    'int': Integer,
    'OUT uint64_t *': IntegerOut,
    'OUT uint32_t *': Integer32Out,
    'char': Char,
    'void': Void,
    'void *': Void,

    'TDB_DATA *': TDB_DATA_P,
    'TDB_DATA': TDB_DATA,
    'uint64_t': Integer,
    'uint32_t': Integer32,
    'uint16_t': Integer,
    'int64_t': Integer,
    'unsigned long int': Integer,
    'struct timeval': Timeval,

    'PyObject *': PyObject,
    }

method_attributes = ['BORROWED', 'DESTRUCTOR','IGNORE']

def dispatch(name, type):
    if not type: return Void()

    type_components = type.split()
    attributes = set()

    if type_components[0] in method_attributes:
        attributes.add(type_components.pop(0))

    type = " ".join(type_components)
    result = type_dispatcher[type](name, type)
    result.attributes = attributes

    return result


class ResultException:
    value = 0
    exception = "PyExc_IOError"

    def __init__(self, check, exception, message):
        self.check = check
        self.exception = exception
        self.message = message

    def write(self, out):
        out.write("\n//Handle exceptions\n")
        out.write("if(%s) {\n    PyErr_Format(PyExc_%s, %s);\n  goto error; \n};\n\n" % (
                self.check, self.exception, self.message))

class Method:
    default_re = re.compile("DEFAULT\(([A-Z_a-z0-9]+)\) =(.+)")
    exception_re = re.compile("RAISES\(([^,]+),\s*([^\)]+)\) =(.+)")
    typedefed_re = re.compile(r"struct (.+)_t \*")

    def __init__(self, class_name, base_class_name, method_name, args, return_type,
                 myclass = None):
        self.name = method_name
        ## myclass needs to be a class generator
        if not isinstance(myclass, ClassGenerator): raise RuntimeError("myclass must be a class generator")

        self.myclass = myclass
        self.docstring = ''
        self.defaults = {}
        self.exception = None
        self.error_set = False
        self.class_name = class_name
        self.base_class_name = base_class_name
        self.args = []
        self.definition_class_name = class_name
        for type,name in args:
            self.add_arg(type, name)

        try:
            self.return_type = dispatch('func_return', return_type)
            self.return_type.attributes.add("OUT")
            self.return_type.original_type = return_type
        except KeyError:
            ## Is it a wrapped type?
            if return_type:
                log("Unable to handle return type %s.%s %s" % (self.class_name, self.name, return_type))
                pdb.set_trace()
            self.return_type = Void()

    def clone(self, new_class_name):
        self.find_optional_vars()

        result = self.__class__(new_class_name, self.base_class_name, self.name,
                                [], 'void *',
                                myclass = self.myclass)
        result.args = self.args
        result.return_type = self.return_type
        result.definition_class_name = self.definition_class_name
        result.defaults = self.defaults
        result.exception = self.exception

        return result

    def find_optional_vars(self):
        for line in self.docstring.splitlines():
            m =self.default_re.search(line)
            if m:
                name = m.group(1)
                value = m.group(2)
                log("Setting default value for %s of %s" % (m.group(1),
                                                            m.group(2)))
                self.defaults[name] = value

            m =self.exception_re.search(line)
            if m:
                self.exception = ResultException(m.group(1), m.group(2), m.group(3))

    def write_local_vars(self, out):
        self.find_optional_vars()

        ## We do it in two passes - first mandatory then optional
        kwlist = """static char *kwlist[] = {"""
        ## Mandatory
        for type in self.args:
            python_name = type.python_name()
            if python_name and python_name not in self.defaults:
                kwlist += '"%s",' % python_name

        for type in self.args:
            python_name = type.python_name()
            if python_name and python_name in self.defaults:
                kwlist += '"%s",' % python_name

        kwlist += ' NULL};\n'

        for type in self.args:
            python_name = type.python_name()
            try:
                out.write(type.definition(default = self.defaults[python_name]))
            except KeyError:
                out.write(type.definition())

        ## Make up the format string for the parse args in two pases
        parse_line = ''
        for type in self.args:
            python_name = type.python_name()
            if type.buidstr and python_name not in self.defaults:
                parse_line += type.buidstr

        parse_line += '|'
        for type in self.args:
            python_name = type.python_name()
            if type.buidstr and python_name in self.defaults:
                parse_line += type.buidstr

        if parse_line != '|':
            ## Now parse the args from python objects
            out.write(kwlist)
            out.write("\nif(!PyArg_ParseTupleAndKeywords(args, kwds, \"%s\", kwlist, " % parse_line)
            tmp = []
            for type in self.args:
                ref = type.byref()
                if ref:
                    tmp.append(ref)

            out.write(",".join(tmp))
            self.error_set = True
            out.write("))\n goto error;\n\n")

    def error_condition(self):
        result = ""
        if "DESTRUCTOR" in self.return_type.attributes:
            result += "talloc_free(self->ctx); self->base = NULL;\n"

        return result +"return NULL;\n";

    def write_definition(self, out):
        args = dict(method = self.name, class_name = self.class_name)
        out.write("\n/********************************************************\nAutogenerated wrapper for function:\n")
        out.write(self.comment())
        out.write("********************************************************/\n")

        out.write("""
static PyObject *py%(class_name)s_%(method)s(py%(class_name)s *self, PyObject *args, PyObject *kwds) {
       PyObject *returned_result, *py_result;
""" % args)

        out.write(self.return_type.definition())

        self.write_local_vars( out);

        out.write("""// Make sure that we have something valid to wrap
if(!self->base) return PyErr_Format(PyExc_RuntimeError, "%(class_name)s object no longer valid");
""" % args)

        ## Precall preparations
        out.write("// Precall preparations\n")
        out.write(self.return_type.pre_call(self))
        for type in self.args:
            out.write(type.pre_call(self))

        out.write("""// Check the function is implemented
  {  void *method = ((%(def_class_name)s)self->base)->%(method)s;
     if(!method || (void *)unimplemented == (void *)method) {
         PyErr_Format(PyExc_RuntimeError, "%(class_name)s.%(method)s is not implemented");
         goto error;
     };
  };
""" % dict(def_class_name = self.definition_class_name, method=self.name,
           class_name = self.class_name))

        out.write("\n// Make the call\n ClearError();")
        call = "((%s)self->base)->%s(((%s)self->base)" % (self.definition_class_name, self.name, self.definition_class_name)
        tmp = ''
        for type in self.args:
            tmp += ", " + type.call_arg()

        call += "%s)" % tmp

        ## Now call the wrapped function
        out.write(self.return_type.assign(call, self))
        if self.exception:
            self.exception.write(out)

        self.error_set = True
        out.write("""//Check for errors
         if(!CheckError(EZero)) {
                char *buffer;
                PyObject *exception = resolve_exception(&buffer);

                PyErr_Format(exception,
                            "%s", buffer);
                ClearError();
                goto error;
         }""")

        out.write("\n// Postcall preparations\n")
        ## Postcall preparations
        out.write(self.return_type.post_call(self))
        for type in self.args:
            out.write(type.post_call(self))

        ## Now assemble the results
        results = [self.return_type.to_python_object()]
        for type in self.args:
            if type.sense == 'OUT_DONE':
                results.append(type.to_python_object(results = results))

        ## If all the results are returned by reference we dont need
        ## to prepend the void return value at all.
        if isinstance(self.return_type, Void) and len(results)>1:
            results.pop(0)

        out.write("\n// prepare results\n")
        ## Make a tuple of results and pass them back
        if len(results)>1:
            out.write("returned_result = PyList_New(0);\n")
            for result in results:
                out.write(result)
                out.write("PyList_Append(returned_result, py_result); Py_DECREF(py_result);\n");
            out.write("return returned_result;\n")
        else:
            out.write(results[0])
            ## This useless code removes compiler warnings
            out.write("returned_result = py_result;\nreturn returned_result;\n");

        ## Write the error part of the function
        if self.error_set:
            out.write("\n// error conditions:\n")
            out.write("error:\n    " + self.error_condition());

        out.write("\n};\n\n")

    def add_arg(self, type, name):
        try:
            t = type_dispatcher[type](name, type)
        except KeyError:
            ## Sometimes types must be typedefed in advance
            try:
                m = self.typedefed_re.match(type)
                type = m.group(1)
                log( "Trying %s for %s" % (type, m.group(0)))
                t = type_dispatcher[type](name, type)
            except (KeyError, AttributeError):
                log( "Unable to handle type %s.%s %s" % (self.class_name, self.name, type))
                return

        ## Here we collapse char * + int type interfaces into a
        ## coherent string like interface.
        try:
            previous = self.args[-1]
            if t.interface == 'integer' and \
                    previous.interface == 'string':

                ## We make a distinction between IN variables and OUT
                ## variables
                if previous.sense == 'OUT':
                    cls = Char_and_Length_OUT
                else:
                    cls = Char_and_Length


                cls = cls(
                    previous.name,
                    previous.type,
                    name, type)

                self.args[-1] = cls

                return
        except IndexError:
            pass

        self.args.append(t)

    def comment(self):
        result = ''
        #result += " {%s (%s)}" % (self.return_type.__class__.__name__, self.return_type.attributes)
        result += self.return_type.original_type+" "+self.class_name+"."+self.name+"("
        args = []
        for type in self.args:
            #result += " {%s (%s)} " %( type.__class__.__name__, type.attributes)
            args.append( type.comment())

        result += ",".join(args) + ");\n"

        return result

    def prototype(self, out):
        out.write("""static PyObject *py%(class_name)s_%(method)s(py%(class_name)s *self, PyObject *args, PyObject *kwds);\n""" % dict(method = self.name, class_name = self.class_name))

class ConstructorMethod(Method):
    ## Python constructors are a bit different than regular methods
    def prototype(self, out):
        out.write("""
static int py%(class_name)s_init(py%(class_name)s *self, PyObject *args, PyObject *kwds);
""" % dict(method = self.name, class_name = self.class_name))

    def write_destructor(self, out):
        ## We make sure that we unlink exactly the reference we need
        ## (the object will persist if it has some other
        ## references). Note that if we just used talloc_free here it
        ## will remove some random reference which may not actually be
        ## the reference we own (which is NULL).
        free = """
    if(self->base) {
        //printf("Unlinking %s@%p\\n", NAMEOF(self->base), self->base);
        talloc_free(self->ctx);
        self->base=NULL;
    };
"""
        out.write("""static void
%(class_name)s_dealloc(py%(class_name)s *self) {
%(free)s
 PyObject_Del(self);
};\n
""" % dict(class_name = self.class_name, free=free))

    def error_condition(self):
        return "return -1;";

    def write_definition(self, out):
        out.write("""static int py%(class_name)s_init(py%(class_name)s *self, PyObject *args, PyObject *kwds) {\n""" % dict(method = self.name, class_name = self.class_name))

        self.write_local_vars(out)

        ## Precall preparations
        for type in self.args:
            out.write(type.pre_call(self))

        ## Now call the wrapped function
        out.write("\nself->ctx = talloc_strdup(NULL, \"%s\");" % self.class_name)
        out.write("\n Py_BEGIN_ALLOW_THREADS\nself->base = CONSTRUCT(%s, %s, %s, self->ctx" % (
                self.class_name,
                self.definition_class_name,
                self.name))
        tmp = ''
        for type in self.args:
            tmp += ", " + type.call_arg()

        self.error_set = True
        out.write("""%s);\nPy_END_ALLOW_THREADS\n
       if(!CheckError(EZero)) {
         char *buffer;
         PyObject *exception = resolve_exception(&buffer);

         PyErr_Format(exception,
                    "%%s", buffer);
         ClearError();
         goto error;
  } else if(!self->base) {
    PyErr_Format(PyExc_IOError, "Unable to construct class %s");
    goto error;
  };
""" % (tmp, self.class_name))

        out.write("  return 0;\n");

        ## Write the error part of the function
        if self.error_set:
            out.write("error:\n    " + self.error_condition());

        out.write("\n};\n\n")

class GetattrMethod(Method):
    def __init__(self, class_name, base_class_name):
        self.class_name = class_name
        self.base_class_name = base_class_name
        self.attributes = []
        self.error_set = True
        self.return_type = Void()
        self.name = "py%s_getattr" % class_name

    def add_attribute(self, attr):
        if attr.name:
            self.attributes.append((self.class_name, attr))

    def clone(self, class_name):
        result = self.__class__(class_name, self.base_class_name)
        result.attributes = self.attributes[:]

        return result

    def prototype(self, out):
        if self.name:
            out.write("""
static PyObject *%(name)s(py%(class_name)s *self, PyObject *name);
""" % self.__dict__)

    def built_ins(self, out):
        """ check for some built in attributes we need to support """
        out.write("""  if(!strcmp(name, "__members__")) {
     PyObject *result = PyList_New(0);
     PyObject *tmp;
     PyMethodDef *i;

     if(!result) goto error;
""")
        ## Add attributes
        for class_name, attr in self.attributes:
            out.write(""" tmp = PyString_FromString("%(name)s");
    PyList_Append(result, tmp); Py_DECREF(tmp);
""" % dict(name = attr.name))

        ## Add methods
        out.write("""

    for(i=%s_methods; i->ml_name; i++) {
     tmp = PyString_FromString(i->ml_name);
    PyList_Append(result, tmp); Py_DECREF(tmp);
    }; """ % self.class_name)

        out.write("""
     return result; 
   }\n""")

    def write_definition(self, out):
        if not self.name: return
        out.write("""
static PyObject *py%(class_name)s_getattr(py%(class_name)s *self, PyObject *pyname) {
  char *name = PyString_AsString(pyname);

  if(!self->base) return PyErr_Format(PyExc_RuntimeError, "Wrapped object no longer valid");
  if(!name) return NULL;
""" % self.__dict__)

        self.built_ins(out)

        for class_name, attr in self.attributes:
            ## what we want to assign
            if self.base_class_name:
                call = "(((%s)self->base)->%s)" % (class_name, attr.name)
            else:
                call = "(self->base->%s)" % (attr.name)

            out.write("""
if(!strcmp(name, "%(name)s")) {
    PyObject *py_result;
    %(python_def)s

    %(python_assign)s
    %(python_obj)s
    return py_result;
};""" % dict(name = attr.name, python_obj = attr.to_python_object(),
             python_assign = attr.assign(call, self),
             python_def = attr.definition(sense='out')))

        out.write("""

  // Hand it off to the python native handler
  return PyObject_GenericGetAttr((PyObject *)self, pyname);
""" % self.__dict__)

        ## Write the error part of the function
        if self.error_set:
            out.write("error:\n" + self.error_condition());

        out.write("}\n\n")

class ProxiedGetattr(GetattrMethod):
    def __init__(self, class_name, base_class_name):
        GetattrMethod.__init__(self, class_name, base_class_name)

    def built_ins(self,out):
        out.write("""  if(!strcmp(name, "__members__")) {
     PyObject *result;
     PyObject *tmp;
     PyMethodDef *i;

     PyErr_Clear();
     // Get the list of members from our proxied object
     result  = PyObject_GetAttrString(self->base->proxied, name);
     if(!result) goto error;
""")
        ## Add attributes
        for class_name, attr in self.attributes:
            out.write(""" tmp = PyString_FromString("%(name)s");
    PyList_Append(result, tmp); Py_DECREF(tmp);
""" % dict(name = attr.name))

        ## Add methods
        out.write("""

    for(i=%s_methods; i->ml_name; i++) {
     tmp = PyString_FromString(i->ml_name);
    PyList_Append(result, tmp); Py_DECREF(tmp);
    }; """ % self.class_name)

        out.write("""
     return result; 
   }\n""")

        out.write(""" /** Just try to get the attribute from our proxied object */  {
   PyObject *result = PyObject_GetAttrString(self->base->proxied, name);
   if(result) return result;
}; """)

class ProxiedMethod(Method):
    def __init__(self, method, myclass):
        self.name = method.name
        self.myclass = myclass
        self.class_name = method.class_name
        self.base_class_name = method.base_class_name
        self.args = method.args
        self.definition_class_name = method.definition_class_name
        self.return_type = method.return_type
        self.docstring = "Proxy for %s" % self.name
        self.defaults = {}
        self.exception = None
        self.error_set = False

    def get_name(self):
        return "py%(class_name)s_%(name)s" % dict(class_name =self.myclass.class_name,
                                                  name = self.name)

    def _prototype(self, out):
        out.write("""
static %(return_type)s %(name)s(%(base_class_name)s self""" % dict(
                return_type = self.return_type.original_type,
                class_name = self.myclass.class_name,
                method = self.name,
                name = self.get_name(),
                base_class_name = self.myclass.base_class_name))

        for arg in self.args:
            out.write(", %s" % (arg.comment()))

        out.write(")")

    def prototype(self, out):
        self._prototype(out)
        out.write(";\n")

    def write_definition(self, out):
        self._prototype(out)
        self._write_definition(out)

    def _write_definition(self, out):
        ## We need to grab the GIL before we do anything
        out.write("""{
      //Grab the GIL so we can do python stuff
      PyGILState_STATE gstate;
      gstate = PyGILState_Ensure();
      """)

        out.write("{\nPyObject *py_result;\n")
        out.write('PyObject *method_name = PyString_FromString("%s");\n' % self.name)
        out.write(self.return_type.returned_python_definition())

        for arg in self.args:
            out.write("PyObject *py_%s=NULL;\n" % arg.name)

        out.write("\n//Obtain python objects for all the args:\n")
        for arg in self.args:
            out.write(arg.to_python_object(result = "py_%s" % arg.name,
                                           sense='proxied', BORROWED=True))

        out.write('if(!((%s)self)->proxied) {\n RaiseError(ERuntimeError, "No proxied object in %s"); goto error;\n};\n' % (self.myclass.class_name, self.myclass.class_name))

        out.write("\n//Now call the method\n")
        out.write("""PyErr_Clear();
py_result = PyObject_CallMethodObjArgs(((%s)self)->proxied,method_name,""" % self.myclass.class_name)
        for arg in self.args:
            out.write("py_%s," % arg.name)

        ## Sentinal
        self.error_set = True
        out.write("""NULL);

/** Check for python errors */
if(PyErr_Occurred()) {
   PyObject *exception_t, *exception, *tb;
   PyObject *str;
   char *str_c;
   char *error_str;
   enum _error_type *error_type = aff4_get_current_error(&error_str);

   // Fetch the exception state and convert it to a string:
   PyErr_Fetch(&exception_t, &exception, &tb);

   str = PyObject_Repr(exception);
   str_c = PyString_AsString(str);

   if(str_c) {
      strncpy(error_str, str_c, BUFF_SIZE-1);
      error_str[BUFF_SIZE-1]=0;
      *error_type = ERuntimeError;
   };
   Py_DECREF(str);
   goto error;
};

""");

        ## Now convert the python value back to a value
        out.write(self.return_type.from_python_object('py_result',self.return_type.name, self, context = "self"))

        out.write("Py_DECREF(py_result);\nPy_DECREF(method_name);\n\n");
        out.write("PyGILState_Release(gstate);\n")

        ## Decref all our python objects:
        for arg in self.args:
            out.write("if(py_%s) { Py_DECREF(py_%s);};\n" %( arg.name, arg.name))

        out.write(self.return_type.return_value('func_return'))
        if self.error_set:
            out.write("\nerror:\n")
            ## Decref all our python objects:
            for arg in self.args:
                out.write("if(py_%s) { Py_DECREF(py_%s);};\n" % (arg.name, arg.name))

            out.write("PyGILState_Release(gstate);\n %s;\n" % self.error_condition())

        out.write("   };\n};\n")

    def error_condition(self):
        return self.return_type.error_value

class StructConstructor(ConstructorMethod):
    """ A constructor for struct wrappers - basically just allocate
    memory for the struct.
    """
    def write_definition(self, out):
        out.write("""static int py%(class_name)s_init(py%(class_name)s *self, PyObject *args, PyObject *kwds) {\n""" % dict(method = self.name, class_name = self.class_name))
        out.write("\nself->ctx = talloc_strdup(NULL, \"%s\");" % self.class_name)
        out.write("\nself->base = talloc(self->ctx, %s);\n" % self.class_name)
        out.write("  return 0;\n};\n\n")

    def write_destructor(self, out):
        out.write("""static void
%(class_name)s_dealloc(py%(class_name)s *self) {
   talloc_free(self->ctx);
};\n
""" % dict(class_name = self.class_name))

class ProxyConstructor(ConstructorMethod):
    def write_destructor(self, out):
        out.write("""static void
%(class_name)s_dealloc(py%(class_name)s *self) {
    if(self->base) {
        // Release the proxied object
        Py_DECREF(self->base->proxied);
        talloc_free(self->ctx);
        self->base = NULL;
    };
};\n

static int %(class_name)s_destructor(void *this) {
  py%(class_name)s *self = (py%(class_name)s *)this;
  Py_DECREF(self->base->proxied);
  return 0;
};
""" % dict(class_name = self.class_name))

    def initialise_attributes(self, out):
        attributes = self.myclass.module.classes[self.base_class_name].attributes.attributes
        for definition_class_name, attribute in attributes:
            out.write("""
{
  // Converting from %(attribute_name)s
  PyErr_Clear();
  PyObject *py_result = PyObject_GetAttrString(self->base->proxied, "%(name)s");

  if(py_result) {
       %(type)s tmp;
       %(from_python_object)s;
       ((%(definition_class_name)s)self->base)->%(name)s = tmp;
       Py_DECREF(py_result);
  };
  PyErr_Clear();
};""" % dict(definition = attribute.definition(), name=attribute.name,
             attribute_name = attribute.__class__.__name__,
             type = attribute.type,
             definition_class_name = definition_class_name,
             from_python_object = attribute.from_python_object(
                        'py_result',"tmp", method=self,
                        context = 'self->base')))


    def write_constructor_proxy(self, out):
        ## Get the base_class constructor
        self.base_cons_method = ProxiedMethod(self.myclass.module.classes[self.base_class_name].constructor, self.myclass)

        self.base_cons_method._prototype(out)
        out.write("{\nPyObject *py_result;\n")
        out.write('PyObject *method_name;')
        out.write("%(class_name)s this = (%(class_name)s)self;\n" % self.__dict__)
        out.write("PyGILState_STATE gstate;\ngstate = PyGILState_Ensure();\n")
        out.write('method_name = PyString_FromString("__class__");\n')
        for arg in self.base_cons_method.args:
            out.write("PyObject *py_%s;\n" % arg.name)

        out.write("\n//Obtain python objects for all the args:\n")
        for arg in self.base_cons_method.args:
            out.write(arg.to_python_object(result = "py_%s" % arg.name, BORROWED=True))

        out.write('if(!((%s)self)->proxied) {\n RaiseError(ERuntimeError, "No proxied object in %s"); goto error;\n};\n' % (self.myclass.class_name, self.myclass.class_name))

        out.write("""
// Enlarge the object size to accomodate the extended class
self = talloc_realloc_size(self, self, sizeof(struct %(base_class_name)s_t));
""" % self.__dict__)
        out.write("\n//Now call the method\n")
        out.write("PyErr_Clear();\npy_result = PyObject_CallMethodObjArgs(((%s)self)->proxied,method_name," % self.myclass.class_name)

        call = ''
        for arg in self.base_cons_method.args:
            call += "py_%s," % arg.name

        ## Sentinal
        self.error_set = True
        call += """NULL"""

        out.write(call + ");\n");
        out.write("""
if(!py_result && PyCallable_Check(this->proxied)) {
   PyErr_Clear();
   py_result = PyObject_CallFunctionObjArgs(((%(name)s)self)->proxied, %(call)s);
};

/** Check for python errors */
if(PyErr_Occurred()) {
   PyObject *exception_t, *exception, *tb;
   PyObject *str;
   char *str_c;
   char *error_str;
   enum _error_type *error_type = aff4_get_current_error(&error_str);

   // Fetch the exception state and convert it to a string:
   PyErr_Fetch(&exception_t, &exception, &tb);

   str = PyObject_Repr(exception);
   str_c = PyString_AsString(str);

   if(str_c) {
      strncpy(error_str, str_c, BUFF_SIZE-1);
      error_str[BUFF_SIZE-1]=0;
      *error_type = ERuntimeError;
   };
   Py_DECREF(str);
   goto error;
};

this->proxied = py_result;
""" % dict(name=self.myclass.class_name, call = call));
        out.write("PyGILState_Release(gstate);\n")
        out.write("\n\nreturn self;\n")
        if self.error_set:
            out.write("error:\n PyGILState_Release(gstate);\ntalloc_free(self); return NULL;\n")

        out.write("};\n\n")

    def write_definition(self, out):
        self.write_constructor_proxy(out)
        out.write("""static int py%(class_name)s_init(py%(class_name)s *self, PyObject *args, PyObject *kwds) {
      PyGILState_STATE gstate = PyGILState_Ensure();
""" % dict(method = self.name, class_name = self.class_name))

        self.write_local_vars(out)

        ## Precall preparations
        for type in self.args:
            out.write(type.pre_call(self))

        ## Make up the call
        self.call = "CONSTRUCT(%(class_name)s, %(base_class_name)s " % self.__dict__

        self.call += ",%s, NULL" % self.base_cons_method.name
        for arg in self.base_cons_method.args:
            self.call += ", %s" % arg.call_arg()

        self.call += ");\n"

        ## Now call the wrapped function
        out.write("""
 self->base = %(call)s

// Take over a copy of the proxied object
 self->base->proxied = proxied;

 /* We take a reference to the proxied object, and the proxied base
 class takes a reference. This way we (the python object) and the
 proxied C class can be freed independantly and only when both are
 freed the proxied object is freed.  */

 Py_INCREF(proxied);
 Py_INCREF(proxied);
 talloc_set_destructor((void*)self->base, %(class_name)s_destructor);
""" % self.__dict__)

        ## Install the handler for the constructor
        out.write("((%(definition_class_name)s)self->base)->%(name)s = %(func)s;\n" % dict(
                definition_class_name = self.base_cons_method.definition_class_name,
                name = self.base_cons_method.name,
                func = self.base_cons_method.get_name()))

        ## Now iterate over all our methods and install handlers:
        for method in self.myclass.methods:
            out.write("((%(definition_class_name)s)self->base)->%(name)s = "
                      "py%(class_name)s_%(name)s;\n" % dict(
                    definition_class_name = method.definition_class_name,
                    name = method.name,
                    class_name = self.myclass.class_name))

        ## Now fill in all attributes from the proxied object. Since
        ## the C struct attribute access is just memory access its
        ## difficult to trap it and refill attributes dynamically from
        ## the python object. Therefore for now we just read all
        ## attributes initially and populate the C struct with them.
        self.initialise_attributes(out)

        out.write("\n   PyGILState_Release(gstate);\n  return 0;\n");

        ## Write the error part of the function
        if self.error_set:
            out.write("error:\n  PyGILState_Release(gstate);\n  " + self.error_condition());

        out.write("\n};\n\n")

class EmptyConstructor(ConstructorMethod):
    def write_definition(self, out):
        out.write("""static int py%(class_name)s_init(py%(class_name)s *self, PyObject *args, PyObject *kwds) {\n""" % dict(method = self.name, class_name = self.class_name))
        out.write("""return 0;};\n\n""")

class ClassGenerator:
    def __init__(self, class_name, base_class_name, module):
        self.class_name = class_name
        self.methods = []
        self.module = module
        self.constructor = EmptyConstructor(class_name, base_class_name,
                                             "Con", [], '', myclass = self)

        self.base_class_name = base_class_name
        self.attributes = GetattrMethod(self.class_name, self.base_class_name)
        self.modifier = ''

    def is_active(self):
        """ Returns true if this class is active and should be generated """
        if self.modifier and ('PRIVATE' in self.modifier \
                                  or 'ABSTRACT' in self.modifier):
            log("%s is not active %s" % (self.class_name, self.modifier))
            return False

        return True

    def clone(self, new_class_name):
        """ Creates a clone of this class - usefull when implementing
        class extensions
        """
        result = ClassGenerator(new_class_name, self.class_name, self.module)
        result.constructor = self.constructor.clone(new_class_name)
        result.methods = [ x.clone(new_class_name) for x in self.methods ]
        result.attributes = self.attributes.clone(new_class_name)

        return result

    def add_method(self, method_name, args, return_type, docstring):
        result = Method(self.class_name, self.base_class_name,
                                   method_name, args, return_type,
                                   myclass = self)

        result.docstring = docstring
        self.methods.append(result)

    def add_attribute(self, attr_name, attr_type):
        try:
            ## All attribute references are always borrowed - that
            ## means we dont want to free them after accessing them
            type_class = dispatch(attr_name, "BORROWED "+attr_type)
        except KeyError:
            log("Unknown attribute type %s for  %s.%s" % (attr_type,
                                                          self.class_name,
                                                          attr_name))
            return

        self.attributes.add_attribute(type_class)

    def add_constructor(self, method_name, args, return_type, docstring):
        if method_name.startswith("Con"):
            self.constructor = ConstructorMethod(self.class_name, self.base_class_name,
                                                 method_name, args, return_type,
                                                 myclass = self)
            self.constructor.docstring = docstring

    def struct(self,out):
        out.write("""\ntypedef struct {
  PyObject_HEAD
  %(class_name)s base;
  void *ctx;
} py%(class_name)s; \n
""" % dict(class_name=self.class_name))

    def code(self, out):
        if not self.constructor:
            raise RuntimeError("No constructor found for class %s" % self.class_name)

        self.constructor.write_destructor(out)
        self.constructor.write_definition(out)
        if self.attributes:
            self.attributes.write_definition(out)

        for m in self.methods:
            m.write_definition(out)

    def initialise(self):
        return "python_wrappers[TOTAL_CLASSES].class_ref = (Object)&__%s;\n" \
            "python_wrappers[TOTAL_CLASSES++].python_type = &%s_Type;\n" % (
            self.class_name, self.class_name)

    def PyMethodDef(self, out):
        out.write("static PyMethodDef %s_methods[] = {\n" % self.class_name)
        for method in self.methods:
            method_name = method.name
            docstring = method.comment() + "\n\n" + method.docstring
            out.write('     {"%s",(PyCFunction)py%s_%s, METH_VARARGS|METH_KEYWORDS, "%s"},\n' % (
                    method_name,
                    self.class_name,
                    method_name, escape_for_string(docstring)))
        out.write("     {NULL}  /* Sentinel */\n};\n")

    def prototypes(self, out):
        """ Write prototype suitable for .h file """
        out.write("""staticforward PyTypeObject %s_Type;\n""" % self.class_name)
        self.constructor.prototype(out)

        if self.attributes:
            self.attributes.prototype(out)
        for method in self.methods:
            method.prototype(out)

    def numeric_protocol(self, out):
        args = {'class':self.class_name}
        out.write("""

static int
%(class)s_nonzero(py%(class)s *v)
{
        return v->base != 0;
};


static PyNumberMethods %(class)s_as_number = {
        (binaryfunc)    0,       /*nb_add*/
        (binaryfunc)    0,       /*nb_subtract*/
        (binaryfunc)    0,       /*nb_multiply*/
                        0,       /*nb_divide*/
                        0,       /*nb_remainder*/
                        0,       /*nb_divmod*/
                        0,       /*nb_power*/
        (unaryfunc)     0,       /*nb_negative*/
        (unaryfunc)     0,       /*tp_positive*/
        (unaryfunc)     0,       /*tp_absolute*/
        (inquiry)       %(class)s_nonzero,   /*tp_nonzero*/
        (unaryfunc)     0,       /*nb_invert*/
                        0,       /*nb_lshift*/
        (binaryfunc)    0,       /*nb_rshift*/
                        0,       /*nb_and*/
                        0,       /*nb_xor*/
                        0,       /*nb_or*/
                        0,       /*nb_coerce*/
                        0,       /*nb_int*/
                        0,       /*nb_long*/
                        0,       /*nb_float*/
                        0,       /*nb_oct*/
                        0,       /*nb_hex*/
        0,                              /* nb_inplace_add */
        0,                              /* nb_inplace_subtract */
        0,                              /* nb_inplace_multiply */
        0,                              /* nb_inplace_divide */
        0,                              /* nb_inplace_remainder */
        0,                              /* nb_inplace_power */
        0,                              /* nb_inplace_lshift */
        0,                              /* nb_inplace_rshift */
        0,                              /* nb_inplace_and */
        0,                              /* nb_inplace_xor */
        0,                              /* nb_inplace_or */
        0,                              /* nb_floor_divide */
        0,                              /* nb_true_divide */
        0,                              /* nb_inplace_floor_divide */
        0,                              /* nb_inplace_true_divide */
        0,                              /* nb_index */
};
"""  % args)
        return "&%(class)s_as_number" % args

    def PyTypeObject(self, out):
        args = {'class':self.class_name, 'module': self.module.name, 
                'getattr_func': 0,
                'docstring': "%s: %s" % (self.class_name, 
                                         escape_for_string(self.docstring))}

        if self.attributes.name:
            args['getattr_func'] = self.attributes.name

        args['numeric_protocol'] = self.numeric_protocol(out)

        out.write("""
static PyTypeObject %(class)s_Type = {
    PyObject_HEAD_INIT(NULL)
    0,                         /* ob_size */
    "%(module)s.%(class)s",               /* tp_name */
    sizeof(py%(class)s),            /* tp_basicsize */
    0,                         /* tp_itemsize */
    (destructor)%(class)s_dealloc,/* tp_dealloc */
    0,                         /* tp_print */
    0,                         /* tp_getattr */
    0,                         /* tp_setattr */
    0,                         /* tp_compare */
    0,                         /* tp_repr */
    %(numeric_protocol)s,      /* tp_as_number */
    0,                         /* tp_as_sequence */
    0,                         /* tp_as_mapping */
    0,                         /* tp_hash */
    0,                         /* tp_call */
    0,                         /* tp_str */
    (getattrofunc)%(getattr_func)s,                         /* tp_getattro */
    0,                         /* tp_setattro */
    0,                         /* tp_as_buffer */
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,        /* tp_flags */
    "%(docstring)s",     /* tp_doc */
    0,	                       /* tp_traverse */
    0,                         /* tp_clear */
    0,                         /* tp_richcompare */
    0,                         /* tp_weaklistoffset */
    0,                         /* tp_iter */
    0,                         /* tp_iternext */
    %(class)s_methods,            /* tp_methods */
    0,                         /* tp_members */
    0,                         /* tp_getset */
    0,                         /* tp_base */
    0,                         /* tp_dict */
    0,                         /* tp_descr_get */
    0,                         /* tp_descr_set */
    0,                         /* tp_dictoffset */
    (initproc)py%(class)s_init,      /* tp_init */
    0,                         /* tp_alloc */
    0,                         /* tp_new */
};
""" % args )

class StructGenerator(ClassGenerator):
    """ A wrapper generator for structs """
    def __init__(self, class_name, base_class_name, module):
        self.class_name = class_name
        self.methods = []
        self.module = module
        self.constructor = StructConstructor(class_name, base_class_name,
                                             'Con', [], "void", myclass = self)
        self.base_class_name = base_class_name
        self.attributes = GetattrMethod(self.class_name, self.base_class_name)

    def struct(self, out):
        out.write("""\ntypedef struct {
  PyObject_HEAD
  %(class_name)s *base;
  void *ctx;
} py%(class_name)s; \n
""" % dict(class_name=self.class_name))

    def initialise(self):
        return ''

class ProxyClassGenerator(ClassGenerator):
    def __init__(self, *args, **kwargs):
        ClassGenerator.__init__(self, *args, **kwargs)
        self.constructor = ProxyConstructor(self.class_name,
                                            self.base_class_name, '__init__',
                                            [('PyObject *', 'proxied')],
                                            'void', myclass = self)

        self.attributes = ProxiedGetattr(self.class_name, self.base_class_name)

    def struct(self, out):
        out.write("""
// The proxied type is an extension of the wrapped type with a pointer
// to the proxied PyObject.
CLASS(%(class_name)s, %(base_class_name)s)
   uint32_t magic;
   PyObject *proxied;
END_CLASS

VIRTUAL(%(class_name)s, %(base_class_name)s) {
} END_VIRTUAL

typedef struct {
  PyObject_HEAD
  %(class_name)s base;
  void *ctx;
} py%(class_name)s; \n
""" % self.__dict__)

    def PyMethodDef(self, out):
        out.write("static PyMethodDef %s_methods[] = {\n" % self.class_name)
        ## For now no methods
        out.write("     {NULL}  /* Sentinel */\n};\n")

class parser:
    class_re = re.compile(r"^([A-Z]+)?\s*CLASS\(([A-Z_a-z0-9]+)\s*,\s*([A-Z_a-z0-9]+)\)")
    method_re = re.compile(r"^\s*([0-9A-Z_a-z ]+\s+\*?)METHOD\(([A-Z_a-z0-9]+),\s*([A-Z_a-z0-9]+),?")
    arg_re = re.compile(r"\s*([0-9A-Z a-z_]+\s+\*?)([0-9A-Za-z_]+),?")
    constant_re = re.compile(r"#define\s+([A-Z_0-9]+)\s+[^\s]+")
    struct_re = re.compile(r"([A-Z]+)\s+typedef struct\s+([A-Z_a-z0-9]+)\s+{")
    proxy_class_re = re.compile(r"^([A-Z]+)?\s*PROXY_CLASS\(([A-Za-z0-9]+)\)")
    end_class_re = re.compile("END_CLASS")
    attribute_re = re.compile(r"^\s*([0-9A-Z_a-z ]+\s+\*?)\s*([A-Z_a-z]+)\s*;")
    comment_re = re.compile(r"^\s*//")
    comment_start_re = re.compile(r"/\*+")
    comment_end_re = re.compile(r"\*+/")
    blank_line_re = re.compile("\s+")
    current_class = None

    def __init__(self, module, verbosity=0):
        self.module = module
        self.current_comment = ''
        self.verbosity = verbosity
        global DEBUG

        DEBUG = verbosity

        io = StringIO.StringIO("""
// Base object
CLASS(Object, Obj)
END_CLASS
""")
        self.parse_fd(io)

    def add_class(self, class_name, base_class_name, class_type, handler, docstring, modifier):
        try:
            self.current_class = self.module.classes[base_class_name].clone(class_name)
        except (KeyError, AttributeError):
            log("Base class %s is not defined !!!!" % base_class_name)
            self.current_class = class_type(class_name, base_class_name, self.module)

        ## Now add the new class to the module object
        self.current_class.docstring = docstring
        self.current_class.modifier = modifier
        self.module.add_class(self.current_class, handler)

    def parse_filenames(self, filenames):
        for f in filenames:
            self._parse(f)

        log("Second pass: Consolidating definitions")
        for f in filenames:
            self._parse(f)

    def _parse(self, filename):
        if filename not in self.module.files:
              self.module.headers += '#include "%s"\n' % filename
              self.module.files.append(filename)

        fd = open(filename)
        self.parse_fd(fd)
        fd.close()

    def parse_fd(self, fd):
        while 1:
            line = fd.readline()
            if not line: break

            ## Handle c++ style comments //
            m = self.comment_re.match(line)
            if m:
                self.current_comment = line[m.end():]
                while 1:
                    line = fd.readline()

                    m = self.comment_re.match(line)
                    if not m:
                        break

                    self.current_comment += line[m.end():]

            ## Multiline C style comments
            m = self.comment_start_re.search(line)
            if m:
                line = line[m.end():]
                while 1:
                    m = self.comment_end_re.search(line)
                    if m:
                        self.current_comment += line[:m.start()]
                        line = fd.readline()
                        break
                    else:
                        self.current_comment += line

                    line = fd.readline()
                    if not line: break

            m = self.constant_re.search(line)
            if m:
                ## We need to determine if it is a string or integer
                if re.search('"', line):
                    ## Its a string
                    self.module.add_constant(m.group(1), 'string')
                else:
                    self.module.add_constant(m.group(1), 'numeric')

            ## Wrap structs
            m = self.struct_re.search(line)
            if m:
                modifier = m.group(1)
                class_name = m.group(2)
                base_class_name = None
                ## Structs may be refered to as a pointer or absolute
                ## - its the same thing ultimatley.

                ## We only wrap structures which are explicitely bound
                if 'BOUND' in modifier:
                    self.add_class(class_name, base_class_name, StructGenerator, StructWrapper,
                                   self.current_comment, modifier)
                    type_dispatcher["%s *" % class_name] = PointerStructWrapper

                continue

            m = self.class_re.search(line)
            if m:
                ## We need to make a new class now... We basically
                ## need to build on top of previously declared base
                ## classes - so we try to find base classes, clone
                ## them if possible:
                modifier = m.group(1)
                class_name = m.group(2)
                base_class_name = m.group(3)
                self.add_class(class_name, base_class_name, ClassGenerator, Wrapper,
                               self.current_comment, modifier)
                type_dispatcher["%s *" % class_name] = PointerWrapper

                continue

            ## Make a proxy class for python callbacks
            m = self.proxy_class_re.search(line)
            if m:
                modifier = m.group(1)
                base_class_name = m.group(2)
                class_name = "Proxied%s" % base_class_name
                try:
                    proxied_class = self.module.classes[base_class_name]
                except KeyError:
                    raise RuntimeError("Need to create a proxy for %s but it has not been defined (yet). You must place the PROXIED_CLASS() instruction after the class definition" % base_class_name)
                self.current_class = ProxyClassGenerator(class_name,
                                                         base_class_name, self.module)
                self.current_class.constructor.args += proxied_class.constructor.args
                self.current_class.docstring = self.current_comment

                ## Create proxies for all these methods
                for method in proxied_class.methods:
                    self.current_class.methods.append(ProxiedMethod(method, self.current_class))

                self.module.add_class(self.current_class, Wrapper)

            m = self.method_re.search(line)
            if self.current_class and m:
                args = []
                method_name = m.group(3)
                return_type = m.group(1).strip()
                ## Ignore private methods
                if return_type.startswith("PRIVATE"): continue

                ## Now parse the args
                offset = m.end()
                while 1:
                    m = self.arg_re.match(line[offset:])
                    if not m:
                        ## Allow multiline definitions if there is \\
                        ## at the end of the line
                        if line.strip().endswith("\\"):
                            line = fd.readline()
                            offset = 0
                            if line:
                                continue

                        break

                    offset += m.end()
                    args.append([m.group(1).strip(), m.group(2).strip()])

                if return_type == self.current_class.class_name and \
                        method_name.startswith("Con"):
                    self.current_class.add_constructor(method_name, args, return_type,
                                                       self.current_comment)
                else:
                    self.current_class.add_method(method_name, args, return_type,
                                                  self.current_comment)

            m = self.attribute_re.search(line)
            if self.current_class and m:
                type = m.group(1)
                name = m.group(2)
                self.current_class.add_attribute(name, type)

            m = self.end_class_re.search(line)
            if m:
                ## Just clear the current class context
                self.current_class = None

            ## We only care about comment immediately above methods
            ## etc as we take them to be documentation. If we get here
            ## the comment is not above anything we care about - so we
            ## clear it:
            self.current_comment = ''

    def write(self, out):
        self.module.write(out)

if __name__ == '__main__':
    p = parser(Module("pyaff4"))
    for arg in sys.argv[1:]:
        p.parse(arg)
        log("second parse")
        p.parse(arg)

    p.write(sys.stdout)

