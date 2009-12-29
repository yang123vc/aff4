import pyaff4
import time,pdb
time.sleep(1)

""" This program demonstrates how to install a new RDFValue handler.

We need to define a python class with the minimum required methods. In order for the resolver to be able to use a python object we need to wrap it in a proxy. The proxy class builds a C class which proxies the python object - this means that when a c function calls the object's method, the proxy will translate it into a python call for the class method.

Once we register the class as an RDFValue implementation, we can only new instances from the resolver by naming its dataType.
"""

oracle = pyaff4.Resolver()

class RDFSpecial:
    """ This is a do nothing class to demonstate python serialization """
    # Our dataType is the name which we will use to serialise this type
    dataType = "aff4:demo:RDFSpecial"
    value = ''

    def encode(self):
        """ This method will be called when we are required to encode
        ourselves to the database. """
        print "Im encoding"
        return self.value

    def decode(self, data):
        """ This method is called when we decode ourselves from the database. """
        print "Decoding %s" % data
        self.value = data

    def serialise(self):
        """ This method is called when we need to write ourselves to
        the RDF serialization. """
        return self.value

    def parse(self, serialised):
        """ This method is called when we need to parse ourselves from
        the rdf serialization. """
        self.value = serialised

    def set(self, value):
        print "Setting value"
        self.value = value

## Register the class for handling RDF objects
oracle.register_rdf_value_class(pyaff4.ProxiedRDFValue(RDFSpecial))

## We can obtain a new value using the dataType now.
value = oracle.new_rdfvalue(RDFSpecial.dataType)
## Note that this method is called in the proxy class, but ends up
## calling the RDFSpecial class instance:
value.set("foobar")

urn = pyaff4.RDFURN()
urn.set("hello")
attr = "attribute"

## Now we can use it in the resolver as normal
oracle.set_value(urn, attr, value)
print oracle.resolve_value(urn, attr, value)

pdb.set_trace()