"""Required operator release receipts shared by Central and its fixture observer."""

from pydantic import StrictBool

from contracts.models import Digest, Model


class ReleaseRegistrationReceipt(Model):
    release_id: Digest


class ReleaseStagingReceipt(Model):
    staged: StrictBool
