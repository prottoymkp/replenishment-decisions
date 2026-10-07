-- One as-of row per SKU and location; internal transfers are inbound only at the destination.
WITH latest AS (
    SELECT * FROM fact_inventory_snapshot WHERE date = ?
), receipts AS (
    SELECT reference_id, SUM(delta_units) AS received_units
    FROM fact_movements
    WHERE movement_type = 'SUPPLIER_RECEIPT' AND date <= ?
    GROUP BY reference_id
), purchase_supply AS (
    SELECT p.sku_id, p.destination_loc AS location_id,
           SUM(GREATEST(0, p.qty_ordered
               - CASE WHEN p.cancellation_date <= ? THEN p.qty_cancelled ELSE 0 END
               - COALESCE(r.received_units, 0))) AS on_order_units
    FROM fact_open_purchase_orders p
    LEFT JOIN receipts r ON p.po_id = r.reference_id
    WHERE p.order_date <= ?
    GROUP BY 1, 2
), transfer_receipts AS (
    SELECT reference_id, SUM(delta_units) AS received_units
    FROM fact_movements
    WHERE movement_type = 'TRANSFER_RECEIPT' AND date <= ?
    GROUP BY reference_id
), transfer_supply AS (
    SELECT t.sku_id, t.destination_loc AS location_id,
           SUM(GREATEST(0, t.qty_dispatched - COALESCE(r.received_units, 0))) AS in_transit_units
    FROM fact_transfers t
    LEFT JOIN transfer_receipts r ON t.transfer_id = r.reference_id
    WHERE t.dispatch_date <= ?
    GROUP BY 1, 2
)
SELECT l.date, l.sku_id, l.location_id, l.on_hand_units, l.allocated_units,
       COALESCE(p.on_order_units, 0) AS supplier_on_order_units,
       COALESCE(t.in_transit_units, 0) AS transfer_in_transit_units,
       l.on_hand_units - l.allocated_units + COALESCE(p.on_order_units, 0)
           + COALESCE(t.in_transit_units, 0) AS inventory_position
FROM latest l
LEFT JOIN purchase_supply p USING (sku_id, location_id)
LEFT JOIN transfer_supply t USING (sku_id, location_id)
ORDER BY l.sku_id, l.location_id
