````markdown
# GoodWe + Amber + AlphaESS export-cost guard

This project controls a **GoodWe GW5000-DNS-30** (Modbus-TCP) using:
- **Amber** prices (import + feed-in) to decide whether exporting is financially bad
- **AlphaESS OpenAPI** telemetry (battery SOC / pGrid / load / charge) to keep export near zero *when export would cost money*

It is designed to **avoid paying to export**, while allowing normal production when exporting earns you money (feedIn <= 0).

---

## Quick start

### 1) Install requirements
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
````

### 2) Create `.env`

Copy `env.example` to `.env` and fill in at least:

* `AMBER_API_KEY`
* `AMBER_SITE_ID`
* `GOODWE_HOST`
* `GOODWE_UNIT`
* `ALPHAESS_APP_ID`
* `ALPHAESS_APP_SECRET`
* `ALPHAESS_SYS_SN` (or use the numeric index shortcut)

```bash
cp env.example .env
nano .env
```

### 3) Run

`start.sh` exports variables from `.env` and runs `control.py`:

```bash
./start.sh
```

Or run directly (make sure env vars are exported):

```bash
set -a; source .env; set +a
python3 control.py
```

### 4) Emergency 'turn limiter off'

`turnoff.py` writes register 256 back to 100%.

```bash
python3 turnoff.py --host 192.168.1.10 --port 502 --unit 247 --value 100
```

---

## What the controller does (logic)

Every loop, it:

1. **Fetches Amber current prices**

* `import` = cents/kWh you pay
* `feedIn` = cents/kWh applied when exporting (**+ means you pay**, **- means you're paid**)

2. Computes:

* `export_costs = (feedIn > EXPORT_COST_THRESHOLD_C)`

  * Default threshold is `0.0c`
  * Meaning: if feed-in becomes positive, exporting costs money.

3. **Fail-safe behaviour**

* If Amber is stale/unavailable beyond `AMBER_MAX_STALE_SEC`, the script assumes exporting **may** be costly.
* If exporting is assumed costly AND AlphaESS data is stale/unavailable, it sets GoodWe output to **0%** (stop generation) to avoid accidental export.

---

## Control modes when export would cost money

### A) If battery is NOT full (SOC < `ALPHAESS_FULL_SOC_PCT`)

Goal: keep the GoodWe producing (and let the battery start charging), but avoid significant grid export.

Behaviour:

* Allow GoodWe to run at **100%** until Alpha reports grid export exceeds:

  * `ALPHAESS_EXPORT_ALLOW_W_BELOW_FULL_SOC` (default 50W)
* If export exceeds that, back off output proportionally based on export amount (feedback loop).

This helps avoid oscillation and helps the battery “grab” the surplus.

### B) If battery IS full (SOC >= `ALPHAESS_FULL_SOC_PCT`) or SOC unknown

Goal: cover house load (and any charge demand), keep export near zero, bias slightly toward importing a small amount.

Behaviour:

* Base target power ≈ `pload + desired_charge`
* Apply pGrid feedback:

  * importing -> allow more PV
  * exporting -> reduce PV
* Apply a small import bias (`ALPHAESS_GRID_IMPORT_BIAS_W`) to avoid tiny accidental exports.

Optional “auto charge headroom”:

* If SOC < `ALPHAESS_AUTO_CHARGE_BELOW_SOC_PCT` and `ALPHAESS_AUTO_CHARGE_W > 0`,
  the controller will assume the battery can absorb up to `ALPHAESS_AUTO_CHARGE_W` and will leave PV headroom accordingly.

---

## When export does NOT cost money

If `export_costs=False`, the controller requests **100% output**.

Whether it also disables GoodWe’s export limit function depends on:

* `GOODWE_ALWAYS_ENABLED=1` (default): keep limiting enabled but set % to 100
* `GOODWE_ALWAYS_ENABLED=0`: disable export limiting when export is allowed

---

## Sign conventions (important)

AlphaESS values can have different sign conventions depending on firmware/endpoint.

This script normalises internally to:

* `pBat`: **+ charging**, **- discharging**
* `pGrid`: **+ import**, **- export**

If your readings look backwards in logs, flip:

* `ALPHAESS_PBAT_POSITIVE_IS_CHARGE`
* `ALPHAESS_PGRID_POSITIVE_IS_IMPORT`

Example:
If the log shows `pgrid=+200W` while you are clearly exporting, set:
`ALPHAESS_PGRID_POSITIVE_IS_IMPORT=0`

---

## GoodWe registers and modes

Two limiter modes exist:

### `GOODWE_EXPORT_LIMIT_MODE=active_pct` (recommended)

* Writes the active power limit percentage into:

  * `GOODWE_ACTIVE_PCT_REG` (default 256)
* Also writes `GOODWE_EXPORT_SWITCH_REG` (default 291) on/off

### `GOODWE_EXPORT_LIMIT_MODE=pct`

* Writes the percentage into:

  * `GOODWE_EXPORT_PCT_REG` (default 292)
  * `GOODWE_EXPORT_PCT10_REG` (default 293) as % × 10
* Also writes `GOODWE_EXPORT_SWITCH_REG` (default 291) on/off

---

## Modbus reliability / reconnect

The Modbus client includes:

* Compatibility across pymodbus versions (`unit=` vs `slave=` vs `device_id=`)
* Auto-reconnect on common socket errors (Broken pipe, reset, timeout, etc.)

Tune with:

* `MODBUS_RECONNECT_ON_ERROR`
* `MODBUS_RECONNECT_MIN_BACKOFF_SEC`
* `MODBUS_RECONNECT_MAX_BACKOFF_SEC`

---

## Tuning / troubleshooting

### `It keeps limiting even when feedIn is negative`

* Confirm your pricing sign:

  * feedIn > 0 means you pay to export
  * feedIn < 0 means you get paid to export
* `EXPORT_COST_THRESHOLD_C` should usually be `0.0`

### `pGrid looks wrong`

Flip `ALPHAESS_PGRID_POSITIVE_IS_IMPORT`.

### `Battery never shows charging/discharging correctly`

Flip `ALPHAESS_PBAT_POSITIVE_IS_CHARGE` or adjust `ALPHAESS_PBAT_IDLE_THRESHOLD_W`.

### `Too many writes / oscillation`

Increase:

* `MIN_SECONDS_BETWEEN_WRITES`
* `LIMIT_SMOOTHING`
* `MIN_PCT_STEP`

### Enable deeper logs

Set:

* `DEBUG=1`

---

## Safety note

This controller can materially affect inverter output.
Test carefully, start with conservative settings, and monitor logs when making changes.

```
