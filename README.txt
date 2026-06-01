Prepared core fixes for MilanTerzic/test

Files included:
- requirements.txt
- config.py
- flows.py
- demand.py

What these fix:
1. ENTSOG BG point mapping
   - `kireevo` and `kalotina` pointDirection keys were swapped.
   - This can mislabel BG entry data and distort the stacked demand chart.

2. Missing data handling
   - `flows.align()` no longer forces all missing values to zero.
   - Missing now stays missing until the demand layer decides whether a fallback is safe.

3. Today fallback logic
   - If today has no data, it falls back to yesterday.
   - If yesterday also has no valid data, it falls back to two days back.
   - It fills only today, does not create duplicate rows, and does not overwrite real today data.

4. June 1 spike protection
   - Derived BG net import components now reuse the latest valid recent history for today if the live value is missing or spikes unrealistically.

5. Streamlit Cloud timezone dependency
   - `tzdata` added to requirements so timezone resolution does not crash the app.

Still recommended after these core fixes:
- patch capacity.py to avoid named timezone conversion to `CET`, or keep `tzdata` installed
- redesign the capacity tab filters to operate on a rolling 3-year range instead of one year at a time
