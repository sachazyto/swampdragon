from django.db.models.signals import pre_delete, m2m_changed
from django.dispatch.dispatcher import receiver
from .pubsub_providers.redis_pubsub_provider import RedisPubSubProvider
from .pubsub_providers.base_provider import PUBACTIONS
from .model_tools import get_property
from .pubsub_providers.model_publisher import publish_model


class SelfPublishModel(object):
    _ignore_changes_for = None
    _should_publish = True

    def __enter__(self):
        self._should_publish = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.save()
        self._should_publish = True

    def __init__(self, *args, **kwargs):
        self._pre_save_state = dict()
        super(SelfPublishModel, self).__init__(*args, **kwargs)
        self._set_ignored_fields()
        relevant_fields = self._get_relevant_fields()
        for field in relevant_fields:
            val = get_property(self, field)
            if hasattr(val, 'all'):
                val = val.all()
            self._pre_save_state[field] = val

    def _set_ignored_fields(self):
        if self.__class__._ignore_changes_for is None:
            self.__class__._ignore_changes_for = set()
            for f in self.serializer_class.get_related_fields():
                self._ignore_changes_for.add(f)

    def _get_relevant_fields(self):
        update_fields = self.serializer_class.update_fields or []
        filter_fields = self.serializer_class.channel_filter_fields or []
        publish_fields = self.serializer_class.publish_fields or []
        relevant_fields = set(update_fields + filter_fields + publish_fields)

        if self.serializer_class.id_field in relevant_fields:
            relevant_fields.remove(self.serializer_class.id_field)
        return relevant_fields #- self._ignore_changes_for

    def _get_changes(self):
        changes = dict()
        for k, v in self._pre_save_state.items():
            val = get_property(self, k)
            if hasattr(val, 'all'):
                val = val.all()
                if v is None:
                    v = []
                diff = list(set(val).symmetric_difference(set(v)))
                if len(diff) > 0:
                    changes[k] = diff
            elif val != v:
                changes[k] = v
        return changes

    serializer_class = None
    publisher_class = RedisPubSubProvider

    def serialize(self):
        serializer = self.serializer_class()
        return serializer.serialize(self)

    def _publish(self, action, changes=None):
        if not self.serializer_class:
            return
        publish_model(self, self.serializer_class(), self.publisher_class(), action, changes)

    def save(self, *args, **kwargs):
        if not self.pk:
            self.action = PUBACTIONS.created
            self.changes = None
        else:
            self.action = PUBACTIONS.updated
            self.changes = self._get_changes()
        super(SelfPublishModel, self).save(*args, **kwargs)

        if self._should_publish:
            self._publish(self.action, self.changes)


@receiver(m2m_changed)
def _self_publish_model_m2m_change(sender, instance, action, **kwargs):
    if isinstance(instance, SelfPublishModel) and action is 'post_clear':
        instance.changes = instance._get_changes()
        instance._publish(instance.action, instance.changes)
    if isinstance(instance, SelfPublishModel) and action is 'post_add':
        instance.changes = instance._get_changes()
        instance._publish(instance.action, instance.changes)


@receiver(pre_delete)
def _self_publish_model_delete(sender, instance, **kwargs):
    if isinstance(instance, SelfPublishModel):
        instance._publish(PUBACTIONS.deleted)