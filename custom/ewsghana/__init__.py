TEST = True
LOCATION_TYPES = ["country", "region", "district", "facility"]

from custom.ewsghana.reports.StockLevelsReport import StockLevelsReport

CUSTOM_REPORTS = (
    ('Custom reports', (
        StockLevelsReport,
    )),
)
