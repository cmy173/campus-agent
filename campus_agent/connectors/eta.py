"""Capabilities around the existing, tested protocols. Sessions are injected."""
from .profile_protocol import eta_profile
from ..profile_fields import PROFILE_KEYS
from ..connector_contracts import ConnectorSpec, FetchResult


class EtaAdapter:
    spec = ConnectorSpec('eta', ('profile',), 'cas+eta')

    def fetch(self, capability, query, session):
        fields, url = eta_profile(session, getattr(session, 'expected_identity', ''))
        field_urls = getattr(session, 'eta_field_urls', {})
        return FetchResult([fields | {'source': url,
            'field_sources':{k:{'source':field_urls.get(k,url)} for k in PROFILE_KEYS if fields.get(k)}}],
            'complete' if all(fields.get(k) for k in PROFILE_KEYS) else 'partial',
            invalidated_fields=tuple(k for k in getattr(session,'eta_invalidated_fields',[]) if k == 'college'))
