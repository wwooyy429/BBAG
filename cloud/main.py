"""Compile-only host for the bounded S2 Research project; no trading strategy."""
from AlgorithmImports import QCAlgorithm


class BbagS2ResearchHost(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2020, 1, 2)
        self.set_end_date(2020, 1, 10)
