# Decision and evaluation method

## As-of discipline

The engine uses the closing inventory snapshot on the review date. Open PO quantities are original orders less receipts and cancellations known on that date. Inbound transfers are dispatches less arrivals known on that date. The simulation's future demand and actual supplier receipts are available only to the replay scorer when their date arrives.

## Forecasts

The first 12 months establish an initial training set. The next six months contain weekly rolling origins for 30-, 60-, and 90-day forecasts. The last six months are reserved for policy replay. Forecast candidates are a 30-day mean and the preceding seven-day pattern repeated forward. Selection uses pooled SKU mean absolute error across stores; displayed diagnostics include WAPE and signed bias.

Stockout observations are censored. A missing sale remains a data error. The history estimator uses earlier available same-weekday observations where possible, then an earlier available-day mean. It never uses a later day to repair an earlier one. Unsupported histories require review. For real data, this estimate is uncertain; users should audit availability reporting before acting on it.

## Purchase and transfer policy

For each store, the transfer target is forecast demand through transfer lead plus seven days, with safety stock approximated from validation errors. The CDC can dispatch only its free stock, and the earliest projected store shortages are considered first. No store-to-store transfers are planned.

The purchasing policy aggregates store demand by SKU. If network free stock plus existing supply due within supplier lead time is below the reorder point, it calculates an order-up-to requirement through lead time plus 30 days. Positive orders are rounded to satisfy both the item minimum quantity and pack multiple. New purchase commitments consume the remaining monthly USD budget. Priority is earliest shortage that an arriving order could address, then modeled gross margin protected per dollar, then SKU ID. A candidate that cannot fit at least one valid minimum order is deferred while smaller feasible candidates continue to be considered.

The baseline applies the same physical stock, forecast, lead-time, MOQ, pack, cash, and weekly transfer rules. Its supplier purchases are prioritized by current days of supply. Both policies are heuristics; neither is claimed to be mathematically optimal.

## Six-month replay and financial interpretation

The replay starts 1 April 2026 from the same inventory and known open orders. New supplier commitments are considered on the first of each month; stores receive a common weekly transfer policy. Both policies face the same seeded latent demand and supplier delay rule. Daily unit fill rate is fulfilled units divided by total demanded units. Lost units are total demand minus fulfilled units. Stockout days count SKU-store-days on which demand exceeded available stock.

Average inventory value is the daily mean of CDC stock, store stock, and owned units in internal transfer, valued at unit cost. Supplier inbound stock is excluded until received. Deferred cash is an unspent commitment, not a saving. Differences between policies are modeled outcomes in a synthetic setting, not empirical company improvements.

Transfer operating cost is estimated separately at $0.75 per dispatched unit in the synthetic demonstration. It does not consume the supplier purchasing budget. The rate is an explicit assumption, not an observed freight bill.

## Limitations

Safety stock uses an approximate normal-error formula and does not guarantee the requested service level. Forecasts are limited to two transparent baselines. The heuristic does not solve a full mixed-integer network problem. The synthetic environment includes seasonal demand, intermittent demand, stockouts, late and partial receipts, but remains a controlled example. Policy rankings are sensitive to cost and lead-time inputs. A real deployment would need supplier confirmation, buyer approval, and monitoring of achieved service and stock accuracy.
