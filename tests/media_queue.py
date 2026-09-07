class RecordingMediaQueue:
    """Test double for the transaction-bound acquisition port."""

    def __init__(self):
        self.enqueued = []

    def enqueue_in(self, conn, job_id):
        self.enqueued.append((conn, job_id))
        return len(self.enqueued)
