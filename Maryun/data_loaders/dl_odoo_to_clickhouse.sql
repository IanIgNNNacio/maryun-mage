-- Docs: https://docs.mage.ai/guides/sql-blocks
SELECT
aml.id as aml_id,
aml.partner_id, --id_proveedor
aml.product_id,
rp.name as proveedor, --nombre proveedor
aml.account_id as id_cuenta,
aa.name->>'es_419' as cuenta_contable, --descripción gasto
aa.account_type as tipo_cuenta, --descripción gasto
aml.move_name as factura, --numero de factura
aml.date as fecha_asiento_contable, --date
aml.invoice_date as fecha_factura, --invoice_date
aml.amount_currency as monto,
aml.analytic_distribution as distribucion --sucursal y distribucion
FROM account_move_line aml
JOIN res_partner rp ON aml.partner_id = rp.id
JOIN account_account aa ON aml.account_id = aa.id
-- WHERE aa.account_type = 'income' OR aa.account_type = 'income_other' OR aa.account_type = 'expense' OR account_type = 'expense_direct_cost' OR account_type = 'off_balance'
-- WHERE aml.id = 158592
WHERE aml.id = 235670 OR
aml.id = 232429