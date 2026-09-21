"""Image cache manager for cached image zvols."""

import time

import nova.conf
from nova.virt import imagecache
from oslo_log import log as logging

from nova_bhyve import exception, images

LOG = logging.getLogger(__name__)

CONF = nova.conf.CONF


class ImageCacheManager(imagecache.ImageCacheManager):
    """Removes cached images that have gone unused."""

    def __init__(self):
        """Start with no image known to be unused."""
        super().__init__()
        self.unused_since = {}

    def update(self, context, all_instances):
        """Remove cached images left unused for the minimum age."""
        if not self.remove_unused_base_images:
            return

        # an instance still spawning has an image_ref but no clone yet
        referenced = {inst.image_ref for inst in all_instances if inst.image_ref}
        now = time.time()
        self.unused_since = {
            image_id: self.unused_since.get(image_id, now)
            for image_id in images.get_cached_images()
            if image_id not in referenced and not images.is_inuse(image_id)
        }

        min_age = CONF.image_cache.remove_unused_original_minimum_age_seconds
        for image_id, since in list(self.unused_since.items()):
            if now - since < min_age:
                continue
            try:
                removed = images.remove_image(image_id)
            except exception.BhyveDriverError as err:
                LOG.warning(
                    "removing cached image %(id)s failed: %(err)s",
                    {"id": image_id, "err": err},
                )
                continue
            if removed:
                LOG.info("removed unused cached image %s", image_id)
                del self.unused_since[image_id]
