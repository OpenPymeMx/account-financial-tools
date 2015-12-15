# -*- coding: utf-8 -*-
##############################################################################
#
#    Odoo, an open source suite of business apps
#    This module copyright (C) 2014-2015 Therp BV (<http://therp.nl>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp.osv import orm
import logging
import psycopg2
from account_check_deposit.account_deposit import res_company


class Company(orm.Model):
    _inherit = 'res.company'

    def reset_chart(self, cr, uid, ids, context=None):
        """
        This method removes the chart of account on the company record,
        including all the related financial transactions.
        """
        journal_obj = self.pool.get('account.journal')
        bank_statement_obj = self.pool.get('account.bank.statement')
        voucher_obj = self.pool.get('account.voucher')
        fiscal_position_tax_obj = self.pool.get('account.fiscal.position.tax')
        reconcile_obj = self.pool.get('account.move.reconcile')
        invoice_obj = self.pool.get('account.invoice')
        invoice_line_obj = self.pool.get('account.invoice.line')
        invoice_tax_obj = self.pool.get('account.invoice.tax')
        move_obj = self.pool.get('account.move')
        fiscal_position_account_obj = self.pool.get(
            'account.fiscal.position.account'
        )
        logger = logging.getLogger('openerp.addons.account_reset_chart')

        def unlink_from_company(model, res_company):
            logger.info(
                'Unlinking all records of model %s for company %s',
                model, res_company.name
            )
            obj = self.pool.get(model)
            if not obj:
                logger.info('Model %s not found', model)
                return
            cr.execute(
                """
                DELETE FROM ir_property ip
                USING {table} tbl
                WHERE value_reference = '{model},' || tbl.id
                    AND tbl.company_id = %s;
                """.format(model=model, table=obj._table),
                (res_company.id,)
            )
            records = obj.search(
                cr, uid, [('company_id', '=', res_company.id)], context=context
            )
            # account_account.unlink() breaks on empty id list
            if records:
                obj.unlink(cr, uid, records, context=context)

        for res_company in self.browse(cr, uid, [ids], context=context):
            journals = journal_obj.search(
                cr, uid, [('company_id', '=', res_company.id)]
            )
            journal_obj.write(
                cr, uid, journals, {'update_posted': True}, context=context
            )
            statements = bank_statement_obj.search(
                cr, uid, [('company_id', '=', res_company.id)], context=context
            )
            bank_statement_obj.button_cancel(
                cr, uid, statements, context=context
            )
            bank_statement_obj.unlink(
                cr, uid, statements, context=context
            )
            try:
                logger.info('Unlinking vouchers.')
                vouchers = voucher_obj.search(
                    cr, uid, [
                        ('company_id', '=', res_company.id),
                        ('state', 'in', ('proforma', 'posted'))
                    ], context=context
                )
                voucher_obj.cancel_voucher(cr, uid, vouchers, context=context)
                vouchers = voucher_obj.search(
                    cr, uid, [('company_id', '=', res_company.id)],
                    context=context
                )
                voucher_obj.unlink(cr, uid, vouchers, context=context)
                logger.info('Unlinking payment orders.')
            except KeyError:
                pass
            try:
                cr.execute(
                    """
                    DELETE FROM payment_line
                    WHERE order_id IN (
                        SELECT id FROM payment_order
                        WHERE company_id = %s
                    );
                    """, (res_company.id,))
                cr.execute(
                    "DELETE FROM payment_order WHERE company_id = %s;",
                    (res_company.id,)
                )
                unlink_from_company('payment.mode')
            except psycopg2.ProgrammingError:
                cr.execute('ROLLBACK')
                pass
            unlink_from_company(
                'account.banking.account.settings', res_company
            )
            unlink_from_company('res.partner.bank', res_company)
            logger.info('Unlinking reconciliations', res_company)
            reconciles = reconcile_obj.search(
                cr, uid,
                [('line_id.company_id', '=', res_company.id)],
                context=context
            )
            reconcile_obj.unlink(cr, uid, reconciles, context=context)
            logger.info('Reset paid invoices\'s workflows')
            paid_invoices = invoice_obj.search(
                cr, uid, [
                    ('company_id', '=', res_company.id),
                    ('state', '=', 'paid')
                ], context=context
            )
            if paid_invoices:
                cr.execute(
                    """
                    UPDATE wkf_instance
                    SET state = 'active'
                    WHERE res_type = 'account_invoice'
                    AND res_id IN %s""", (tuple(paid_invoices.ids),)
                )
                cr.execute(
                    """
                    UPDATE wkf_workitem
                    SET act_id = (
                        SELECT res_id FROM ir_model_data
                        WHERE module = 'account'
                            AND name = 'act_open')
                    WHERE inst_id IN (
                        SELECT id FROM wkf_instance
                        WHERE res_type = 'account_invoice'
                        AND res_id IN %s)
                    """, (tuple(paid_invoices.ids),)
                )
                paid_invoices.signal_workflow('invoice_cancel')

            invoice_ids = invoice_obj.search(
                cr, uid, [('company_id', '=', res_company.id)]
            )
            if invoice_ids:
                logger.info('Unlinking invoices')
                line_ids = invoice_line_obj.search(
                    cr, uid, [('invoice_id', 'in', invoice_ids)],
                    context=context
                )
                invoice_line_obj.unlink(cr, uid, line_ids, context=context)
                invoice_tax_ids = invoice_tax_obj.search(
                    cr, uid, [('invoice_id', 'in', invoice_ids)],
                    context=context
                )
                invoice_tax_obj.unlink(
                    cr, uid, invoice_tax_ids, context=context
                )
                cr.execute(
                    """
                    DELETE FROM account_invoice
                    WHERE id IN %s""", (tuple(invoice_ids),)
                )

            logger.info('Unlinking moves')
            moves_ids = move_obj.search(
                cr, uid, [('company_id', '=', res_company.id)], context=context
            )
            if moves_ids:
                cr.execute(
                    """UPDATE account_move SET state = 'draft'
                       WHERE id IN %s""", (tuple(moves_ids),)
                )
            move_obj.unlink(cr, uid, moves_ids, context=context)
            fiscal_position_tax_ids = fiscal_position_tax_obj.search(
                cr, uid, [
                    '|', ('tax_src_id.company_id', '=', res_company.id),
                    ('tax_dest_id.company_id', '=', res_company.id)],
                context=context
            )
            fiscal_position_tax_obj.unlink(
                cr, uid, fiscal_position_tax_ids, context=context
            )
            fiscal_position_account_ids = fiscal_position_account_obj.search(
                cr, uid, [
                    '|', ('account_src_id.company_id', '=', res_company.id),
                    ('account_dest_id.company_id', '=', res_company.id)
                ], context=context
            )
            fiscal_position_account_obj.unlink(
                cr, uid, fiscal_position_account_ids, context=context
            )
            unlink_from_company('account.fiscal.position', res_company)
            unlink_from_company('account.analytic.line', res_company)
            unlink_from_company('account.tax', res_company)
            unlink_from_company('account.tax.code', res_company)
            unlink_from_company('account.journal', res_company)
            unlink_from_company('account.account', res_company)
        return True
