# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import uuid
from decimal import Decimal

from trytond.model import (ModelSQL, ModelView, fields)
from trytond.pool import PoolMeta, Pool
from trytond.pyson import Eval
from redsys import Client


class PaymentJournal(metaclass=PoolMeta):
    __name__ = 'account.payment.journal'

    redsys_account = fields.Many2One(
        'account.payment.redsys.account', 'Account', ondelete='RESTRICT',
        domain=[
            ('currency', '=', Eval('currency')),
        ],
        states={
            'required': Eval('process_method') == 'redsys',
            'invisible': Eval('process_method') != 'redsys',
        })

    @classmethod
    def __setup__(cls):
        super().__setup__()
        redsys_method = ('redsys', 'Redsys')
        if redsys_method not in cls.process_method.selection:
            cls.process_method.selection.append(redsys_method)



class PaymentGroup(metaclass=PoolMeta):
    __name__ = 'account.payment.group'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons['succeed']['invisible'] |= (
            Eval('process_method') == 'redsys')


class Payment(metaclass=PoolMeta):
    __name__ = 'account.payment'

    redsys_uuid = fields.Char('UUID', readonly=True)
    redsys_reference_gateway = fields.Char('Reference Gateway',
        states={'readonly': Eval('state') != 'draft'}, depends=['state'])
    redsys_authorisation_code = fields.Char('Authorisation Code',
        states={'readonly': Eval('state') != 'draft'}, depends=['state'])
    redsys_gateway_log = fields.Text("Gateway Log", depends=['state'],
        states={'readonly': Eval('state') != 'draft'})

    @staticmethod
    def default_redsys_uuid():
        return '%s' % uuid.uuid4()

    @classmethod
    def create_redsys_payment(cls, reference, origin, redsys_reference, party,
            amount, currency, payment_journal, merchant_url, url_ok, url_ko):
        pool = Pool()
        Payment = pool.get('account.payment')

        sandbox = False
        if payment_journal.redsys_account.mode == 'sandbox':
            sandbox = True

        # cancel old possible transactions not used
        payments = Payment.search([
            ('origin', '=', origin),
            ('state', '=', 'draft'),
            ])
        if payments:
            Payment.fail(payments)

        payment = Payment()
        payment.description = reference
        payment.origin = origin
        payment.journal = payment_journal
        payment.redsys_reference_gateway = redsys_reference
        payment.party = party
        payment.currency = currency
        payment.amount = amount
        payment.save()

        merchant_code = payment_journal.redsys_account.merchant_code
        merchant_secret_key = payment_journal.redsys_account.secret_key

        # In the redsys documentation says that if we are doing tests in the
        # sandbox enviroment the maximum amount is 10.
        # We set "hardcoded" an amount of 9 when the redsys account is in
        # sandbox mode
        if sandbox:
            amount = Decimal(0.01)

        values = {
            'DS_MERCHANT_AMOUNT': amount,
            'DS_MERCHANT_CURRENCY': payment_journal.redsys_account.redsys_currency,
            'DS_MERCHANT_ORDER': redsys_reference,
            'DS_MERCHANT_PRODUCTDESCRIPTION': reference,
            'DS_MERCHANT_TITULAR': payment_journal.redsys_account.merchant_name,
            'DS_MERCHANT_MERCHANTCODE': merchant_code,
            'DS_MERCHANT_MERCHANTURL': merchant_url,
            'DS_MERCHANT_URLOK': url_ok,
            'DS_MERCHANT_URLKO': url_ko,
            'DS_MERCHANT_MERCHANTNAME': payment_journal.redsys_account.merchant_name,
            'DS_MERCHANT_TERMINAL': payment_journal.redsys_account.terminal,
            'DS_MERCHANT_TRANSACTIONTYPE': payment_journal.redsys_account.transaction_type,
            }
        redsyspayment = Client(business_code=merchant_code,
            secret_key=merchant_secret_key, sandbox=sandbox)
        return redsyspayment.redsys_generate_request(values)

    @classmethod
    def redsys_ipn(cls, payment_journal, merchant_parameters, signature):
        pool = Pool()
        Payment = pool.get('account.payment')
        """
        Signal Redsys confirmation payment

        Redsys request form data:
            - Ds_Date
            - Ds_SecurePayment
            - Ds_Card_Country
            - Ds_AuthorisationCode
            - Ds_MerchantCode
            - Ds_Amount
            - Ds_ConsumerLanguage
            - Ds_Response
            - Ds_Order
            - Ds_TransactionType
            - Ds_Terminal
            - Ds_Signature
            - Ds_Currency
            - Ds_Hour
        """
        sandbox = False
        if payment_journal.redsys_account.mode == 'sandbox':
            sandbox = True

        merchant_code = payment_journal.redsys_account.merchant_code
        merchant_secret_key = payment_journal.redsys_account.secret_key

        redsyspayment = None
        redsyspayment = Client(business_code=merchant_code,
            secret_key=merchant_secret_key, sandbox=sandbox)
        valid_signature = redsyspayment.redsys_check_response(
            signature.encode('utf-8'), merchant_parameters.encode('utf-8'))
        if not valid_signature:
            #TODO: handle errors in voyager
            return '500'

        merchant_parameters = redsyspayment.decode_parameters(merchant_parameters)

        reference = merchant_parameters.get('Ds_Order')
        authorisation_code = merchant_parameters.get('Ds_AuthorisationCode')
        amount = merchant_parameters.get('Ds_Amount', 0)
        response = merchant_parameters.get('Ds_Response')

        log = "\n".join([('%s: %s' % (k, v)) for k, v in
            merchant_parameters.items()])

        # Search payment
        payments = Payment.search([
            ('redsys_reference_gateway', '=', reference),
            ('state', '=', 'draft'),
            ], limit=1)
        if payments:
            payment, = payments
            payment.redsys_authorisation_code = authorisation_code
            payment.amount = Decimal(amount)/100
            payment.redsys_gateway_log = log
            payment.save()
        else:
            payment = Payment()
            payment.description = reference
            payment.redsys_authorisation_code = authorisation_code
            payment.journal = payment_journal
            payment.redsys_reference_gateway = reference
            payment.amount = Decimal(amount)/100
            payment.redsys_gateway_log = log
            payment.save()

        # Process transaction 0000 - 0099: Done
        if int(response) < 100:
            Payment.succeed([payment])
            return response
        Payment.fail([payment])
        return response


class Account(ModelSQL, ModelView):
    "Redsys Account"
    __name__ = 'account.payment.redsys.account'

    name = fields.Char('Name', required=True)
    currency = fields.Many2One('currency.currency', "Currency", required=True)
    mode = fields.Selection([
        ('live', 'Live'),
        ('sandbox', 'Sandbox'),
        ], 'Mode', required=True)
    merchant_name = fields.Char('Merchant Name', required=True,
        help='Redsys Merchant Name')
    merchant_code = fields.Char('Merchant Code', required=True,
        help='Redsys Merchant Code')
    secret_key = fields.Char('Secret Key', required=True,
        help='Redsys Secret Key')
    terminal = fields.Integer('Terminal', required=True, help='Redsys Terminal')
    redsys_currency = fields.Integer('Redsys Currency', required=True,
        help='Redsys Currency')
    transaction_type = fields.Integer('Transaction Type', required=True,
        help='Redsys Transaction Type')
    sequence = fields.Many2One('ir.sequence', 'Sequence',
        #required=True,
        domain=[
            ('company', 'in',
            [Eval('context', {}).get('company', -1), None]),
        ], help='Redsys Sequence. Min. 4N. Max. 12 AN')

    @staticmethod
    def default_mode():
        return 'live'

    #TODO: handle operations

# TODO: create functions to that voyager can use
