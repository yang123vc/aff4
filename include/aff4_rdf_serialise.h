/*
** aff4_rdf_serialise.h
**
*/

#ifndef         AFF4_RDF_SERIALISE_H_
# define        AFF4_RDF_SERIALISE_H_

#include "aff4_rdf.h"
#include "aff4_io.h"

/***** Following is an implementation of a serialiser */
CLASS(RDFSerializer, Object)
     raptor_serializer *rdf_serializer;
     raptor_iostream *iostream;
     FileLikeObject fd;
     int count;

     char buff[BUFF_SIZE*2];
     int i;

     Cache attributes;
     Resolver resolver;
     RDFSerializer METHOD(RDFSerializer, Con, char *base_urn, \
                          FileLikeObject fd, Resolver resolver);
     int METHOD(RDFSerializer, serialize_urn, RDFURN urn);
     int METHOD(RDFSerializer, serialize_statement, Object *iter, RDFURN urn, \
                RDFURN attribute);
     void METHOD(RDFSerializer, set_namespace, char *prefix, char *namespc);
     DESTRUCTOR void METHOD(RDFSerializer, close);
END_CLASS

CLASS(RDFParser, Object)
     char message[BUFF_SIZE];
     jmp_buf env;

     RDFURN urn;
     RDFURN volume_urn;

     Cache member_cache;
     Resolver resolver;
     void METHOD(void *, triples_handler, const raptor_statement *triple);
     void METHOD(void *, message_handler, raptor_locator* locator, \
                 const char *message);

// Parses data stored in fd using the format specified. fd is assumed
// to contain a base URN specified (or NULL if non specified).
     int METHOD(RDFParser, parse, FileLikeObject fd, char *format, char *base);
RDFParser METHOD(RDFParser, Con, Resolver resolver);
END_CLASS

#endif       /* !AFF4_RDF_SERIALISE_H_ */
