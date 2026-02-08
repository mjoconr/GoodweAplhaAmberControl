select count(*) as rows_10m
from events
where ts_epoch_ms > (strftime('%s','now') - 600) * 1000;
