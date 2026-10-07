# Data dictionary and file contract

All files are UTF-8 CSV or named sheets in one `.xlsx` workbook. IDs are text. Dates are `YYYY-MM-DD`; costs and prices are USD with two decimals. Quantities are integer units. There is one CDC and three stores in the demonstration. Custom files may use a different number of stores, but must have exactly one CDC.

| Table / grain | Required fields | Meaning |
|---|---|---|
| `dim_items` / SKU | `sku_id`, `style`, `color`, `size`, `category`, `supplier_id`, `unit_cost_usd`, `wholesale_price_usd`, `moq_units`, `pack_multiple`, `supplier_lead_time_days` | Supplier and item rules. MOQ is the minimum positive buy; pack multiple is the allowed increment. |
| `dim_locations` / location | `location_id`, `location_name`, `location_type` | `location_type` is `CDC` or `Store`. |
| `fact_sales` / date × SKU × store | `date`, `sku_id`, `location_id`, `units_sold`, `stockout_flag` | Observed sales and an explicit indicator that true demand could have been censored. Recorded zero is different from missing. |
| `fact_inventory_snapshot` / date × SKU × location | `date`, `sku_id`, `location_id`, `on_hand_units`, `allocated_units` | Closing stock and reservations. Allocated units must not exceed on-hand. |
| `fact_open_purchase_orders` / PO line | `po_id`, `sku_id`, `destination_loc`, `order_date`, `expected_delivery_date`, `qty_ordered`, `qty_cancelled`, `cancellation_date` | Full PO-line history, including lines still open. Cancellation affects historical positions only on or after its date. Receipts are movements linked by `reference_id`. |
| `fact_movements` / receipt, dispatch, adjustment event | `date`, `sku_id`, `location_id`, `movement_type`, `delta_units`, `reference_id` | Signed local stock change. Supported types include `SUPPLIER_RECEIPT`, `TRANSFER_DISPATCH`, `TRANSFER_RECEIPT`, and `ADJUSTMENT`. Sales are recorded separately and subtracted once. |
| `fact_transfers` / dispatched transfer | `transfer_id`, `sku_id`, `source_loc`, `destination_loc`, `dispatch_date`, `expected_arrival_date`, `qty_dispatched` | Dispatch reduces CDC stock; arrival increases store stock. The transfer remains owned inventory while in transit. |

At a `(date, sku_id, location_id)` grain, `inventory_position = on_hand + open_supplier_supply + dispatched_inbound_transfers - allocated`. Incoming transfers are counted only at their destination, after the source dispatch has reduced source on-hand. Reconciliation checks that today's closing on-hand equals yesterday's closing on-hand plus local movements minus sales.

`truth_demand.csv` is generated solely as a separate scoring file for the synthetic case. Do not include it in uploads or planning inputs. It contains latent demand on stockout days and is never used to choose a forecast or action.
