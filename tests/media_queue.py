from central.media_queue import QueueReceipt


class RecordingMediaQueue:
    """Test double for the transaction-bound media task port."""

    def __init__(self):
        self.enqueued = []
        self.refreshes = []
        self.previews = []

    def enqueue_in(self, conn, job_id):
        self.enqueued.append((conn, job_id))
        return len(self.enqueued)

    def enqueue_refresh_in(self, conn, source_ref):
        coalesced = any(item[1] == source_ref for item in self.refreshes)
        self.refreshes.append((conn, source_ref))
        return QueueReceipt(coalesced=coalesced)

    def enqueue_preview_in(self, conn, request_id):
        self.previews.append((conn, request_id))
        return len(self.previews)
