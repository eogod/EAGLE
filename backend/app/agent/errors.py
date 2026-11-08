"""shared agent exceptions, imported by every stage driver"""


class RunCancelled(Exception):
    """operator cancelled the run; partial results are not persisted as a draft"""
