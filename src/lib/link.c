#include "zip.h"

AFFObject Link_Con(AFFObject self, char *urn) {
  Link this = (Link)self;

  if(urn) {
    char *target = CALL(oracle, resolve, urn, "aff2:target");
    if(!target) {
      RaiseError(ERuntimeError, "%s unable to resolve the aff2:link_to property?", urn);
      goto error;
    };

    return CALL(oracle, open, self, target);
  } else {
    this->__super__->Con(self, urn);
  };

  return self;
 error:
  talloc_free(self);
  return NULL;
};

AFFObject Link_finish(AFFObject self) {
  return self;
};

// A convenience function to set up a link between a target urn to a
// friendly name.
void Link_link(Link self, Resolver oracle, char *storage_urn,
	       char *target, char *friendly_name) {
  AFFObject this = (AFFObject)self;
  if(storage_urn) {
    ZipFile zipfile = (ZipFile)CALL(oracle, open, self, storage_urn);
    char tmp[BUFF_SIZE];
    FileLikeObject fd;
    char *properties;

    if(!zipfile) {
      RaiseError(ERuntimeError, "Unable to get storage container %s", storage_urn);
      return;
    };

    // Add a reverse connection (The link urn is obviously not unique).
    CALL(oracle, add, friendly_name, "aff2:target", target);
    CALL(oracle, add, friendly_name, "aff2:type", "link");

    snprintf(tmp, BUFF_SIZE, "%s/properties", friendly_name);

    fd = CALL((ZipFile)zipfile, open_member, tmp, 'w', NULL, 0, ZIP_STORED);
    if(fd) {
      properties = CALL(oracle, export, friendly_name);
      CALL(fd, write, ZSTRING_NO_NULL(properties));
      talloc_free(properties);

      CALL(fd, close);
    };
    CALL(oracle, cache_return, (AFFObject)zipfile);
  };
};

VIRTUAL(Link, AFFObject)
     VMETHOD(super.Con) = Link_Con;
     VMETHOD(super.finish) = Link_finish;
     VMETHOD(link) = Link_link;
END_VIRTUAL