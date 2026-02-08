select slimmed, count(*) from events group by slimmed;

select datetime(min(ts_epoch_ms)/1000,'unixepoch') as oldest_unslimmed
from events
where slimmed=0;

