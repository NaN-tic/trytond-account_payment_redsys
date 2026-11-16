# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import uuid
import json
import requests
from decimal import Decimal
from trytond.modules.company.model import CompanyMultiValueMixin
from trytond.model import ModelSQL, ModelView, Workflow, fields
from trytond.pool import PoolMeta, Pool
from trytond.pyson import Eval, Id
from trytond.transaction import Transaction
from redsys import Client

REDSYS_SIGNATURE_VERSION = 'HMAC_SHA256_V1'


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
    redsys_refunds = fields.One2Many(
        'account.payment.redsys.refund', 'payment', "Refunds",
        states={ 'invisible': Eval('process_method') != 'redsys' })

    @staticmethod
    def default_redsys_uuid():
        return '%s' % uuid.uuid4()

    @staticmethod
    def get_redsys_client(payment_journal, paymethod=None):
        sandbox = (payment_journal.redsys_account.mode == 'sandbox')

        merchant_code = payment_journal.redsys_account.merchant_code
        merchant_secret_key = payment_journal.redsys_account.secret_key

        return Client(sandbox=sandbox, paymethod=paymethod,
            business_code=merchant_code, secret_key=merchant_secret_key)

    @classmethod
    def get_verified_redsys_data(cls, merchant_parameters, signature,
            signature_version, journal):
        """Return signed Redsys data from parameters collected by the caller.

        Raise ValueError for missing, malformed or unauthenticated parameters.
        This does not look up a payment or establish whether it succeeded.
        """
        if not (merchant_parameters and signature and signature_version):
            raise ValueError('Missing Redsys parameters')
        if signature_version != REDSYS_SIGNATURE_VERSION:
            raise ValueError('Unsupported Redsys signature version')

        client = cls.get_redsys_client(journal)
        try:
            valid_signature = client.redsys_check_response(
                signature.encode('utf-8'), merchant_parameters.encode('utf-8'))
        except (AssertionError, AttributeError, KeyError, TypeError,
                ValueError) as exception:
            raise ValueError('Malformed Redsys parameters') from exception
        if not valid_signature:
            raise ValueError('Invalid Redsys signature')

        data = client.decode_parameters(merchant_parameters)
        if not isinstance(data, dict):
            raise ValueError('Redsys merchant parameters must be a JSON object')
        order = data.get('Ds_Order')
        if (not isinstance(order, str) or not order
                or not order.isascii() or not order.isalnum() or len(order) > 12):
            raise ValueError('Invalid Redsys order reference')

        response = str(data.get('Ds_Response', ''))
        if (not response.isascii() or not response.isdigit()
                or not 0 <= int(response) <= 9999):
            raise ValueError('Invalid Redsys response code')
        return data

    @classmethod
    def create_redsys_payment(cls, reference, origin, redsys_reference, party,
            amount, currency, payment_journal, merchant_url, url_ok, url_ko,
            paymethod=None):
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
        payment.reference = reference
        payment.kind = 'receivable'
        payment.origin = origin
        payment.journal = payment_journal
        payment.redsys_reference_gateway = redsys_reference
        payment.party = party
        payment.currency = currency
        payment.amount = amount
        payment.save()

        merchant_code = payment_journal.redsys_account.merchant_code

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

        redsyspayment = cls.get_redsys_client(payment_journal, paymethod)
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
        redsyspayment = cls.get_redsys_client(payment_journal)
        valid_signature = redsyspayment.redsys_check_response(
            signature.encode('utf-8'), merchant_parameters.encode('utf-8'))
        if not valid_signature:
            return '500'

        merchant_parameters = redsyspayment.decode_parameters(merchant_parameters)

        payments = Payment.search([
            ('redsys_reference_gateway', '=', merchant_parameters.get('Ds_Order')),
            ('journal', '=', payment_journal),
            ], limit=1)
        if not payments:
            return '500'
        payment, = payments
        return payment.process_redsys_data(merchant_parameters)

    def process_redsys_data(self, data):
        """Process this payment using already verified Redsys data."""
        pool = Pool()
        Payment = pool.get('account.payment')
        Group = pool.get('account.payment.group')

        Payment.lock((self,))
        response = data.get('Ds_Response')
        if self.state != 'draft':
            if self.state not in {'succeeded', 'failed'}:
                return '500'
            if (self.state == 'succeeded') != (int(response) < 100):
                return '500'
            if any(refund.reason == 'invalid'
                    for refund in self.redsys_refunds):
                return '500'
            return response
        self.redsys_authorisation_code = data.get('Ds_AuthorisationCode')
        self.amount = Decimal(data.get('Ds_Amount', 0))/100
        self.redsys_gateway_log = "\n".join([
            '%s: %s' % (k, v) for k, v in data.items()])
        self.save()

        # Process transaction 0000 - 0099: Done
        Payment.submit([self])
        # Create payment group (needed in account.payment)
        # TODO: The group is optional in version 7.6 and above:
        # https://foss.heptapod.net/tryton/tryton/-/commit/55db7836e13205b986ba644017973ba1e2024bd6
        group_values = {
            'company': self.company.id,
            'journal': self.journal.id,
            'kind': self.kind,
        }
        group = Group(**group_values)
        group.save()
        self.group = group
        self.save()
        Payment.proceed([self])

        if int(response) < 100:
            Payment.succeed([self])
            if not self.__valid_redsys_payment(data):
                return '500'
        else:
            Payment.fail([self])
        return response

    def __valid_redsys_payment(self, merchant_parameters):
        pool = Pool()
        Sale = pool.get('sale.sale')
        Refund = pool.get('account.payment.redsys.refund')

        transaction = Transaction()

        sale = self.origin
        amount = merchant_parameters.get('Ds_Amount', 0)
        amount = Decimal(amount) / 100

        # On draft state, some amounts are not computed.
        if sale.state == 'draft':
            with transaction.set_context(_skip_warnings=True):
                Sale.quote((sale,))

        if amount == sale.total_amount:
            return True

        refund = Refund(payment=self, state='draft', reason='invalid')
        refund.save()
        refund.__queue__.cancel_redsys_payment()

        return False


class Account(ModelSQL, ModelView, CompanyMultiValueMixin):
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
            ('sequence_type', '=', Id(
                'account_payment_redsys', 'sequence_type_payment_redsys'))
        ], help='Redsys Sequence. Min. 4N. Max. 12 AN')
    mismatch = fields.Numeric('Mismatch (%)', digits=(8, 4),
        required=True)

    @staticmethod
    def default_mode():
        return 'live'

    @staticmethod
    def default_mismatch():
        return 0


class RedsysRefund(Workflow, ModelSQL, ModelView):
    "Redsys Refund"
    __name__ = 'account.payment.redsys.refund'

    payment = fields.Many2One('account.payment', 'Payment', required=True,
        domain=[
            ('process_method', '=', 'redsys'),
            ('redsys_reference_gateway', '!=', None),
            ('state', '=', 'succeeded'),
            ],
        states={
            'readonly': Eval('state') != 'draft',
            })
    reason = fields.Selection([
        (None, ""),
        ('invalid', 'Invalid Refund'),
        ('requested_by_customer', "Requested by Customer"),
        ], "Reason", states={
            'readonly': Eval('state') != 'draft',
            })
    state = fields.Selection([
        ('draft', "Draft"),
        ('succeeded', "Succeeded"),
        ('failed', "Failed"),
        ], "State", readonly=True, sort=False)

    redsys_error_message = fields.Text("Redsys Error Message", readonly=True,
        states={ 'invisible': Eval('state') == 'succeeded' })
    redsys_error_code = fields.Char("Redsys Error Code", readonly=True,
        states={ 'invisible': Eval('state') == 'succeeded' })

    currency = fields.Function(
        fields.Many2One('currency.currency', "Currency"),
        'on_change_with_currency')
    company = fields.Function(
        fields.Many2One('company.company', "Company"),
        'on_change_with_company')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('payment')
        cls._transitions |= set((
            ('draft', 'succeeded'),
            ('draft', 'failed'),
            ('succeeded', 'draft'),
            ))
        cls._buttons.update({
            'draft': {
                'invisible': Eval('state') == 'draft',
                'icon': 'tryton-back',
                'depends': ['state'],
                },
            'succeed': {
                'invisible': Eval('state') != 'draft',
                'icon': 'tryton-forward',
                'depends': ['state'],
                },
            'fail': {
                'invisible': Eval('state') != 'draft',
                'icon': 'tryton-cancel',
                'depends': ['state'],
                },
            })

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_reason():
        return 'requested_by_customer'

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, refunds):
        pass

    @classmethod
    @ModelView.button
    @Workflow.transition('succeeded')
    def succeed(cls, refunds):
        pass

    @classmethod
    @ModelView.button
    @Workflow.transition('failed')
    def fail(cls, refunds):
        pass

    @fields.depends('payment', '_parent_payment.currency')
    def on_change_with_currency(self, name=None):
        return self.payment.currency if self.payment else None

    @fields.depends('payment', '_parent_payment.company')
    def on_change_with_company(self, name=None):
        return self.payment.company if self.payment else None

    def cancel_redsys_payment(self, transaction_type='45'): # Cancellation of authorization (payment)
        pool = Pool()
        Refund = pool.get('account.payment.redsys.refund')

        payment = self.payment
        merchant_code = payment.journal.redsys_account.merchant_code
        sandbox = (payment.journal.redsys_account.mode == 'sandbox')

        # In the redsys documentation says that if we are doing tests in the
        # sandbox enviroment the maximum amount is 10.
        # We set "hardcoded" an amount of 9 when the redsys account is in
        # sandbox mode
        if sandbox:
            amount = Decimal(0.01)
        else:
            amount = payment.amount

        values = {
            'DS_MERCHANT_ORDER': payment.redsys_reference_gateway,
            'DS_MERCHANT_MERCHANTCODE': merchant_code,
            'DS_MERCHANT_TERMINAL': payment.journal.redsys_account.terminal,
            'DS_MERCHANT_TRANSACTIONTYPE': transaction_type,
            'DS_MERCHANT_CURRENCY': payment.journal.redsys_account.redsys_currency,
            'DS_MERCHANT_AMOUNT': amount,
            }

        client = payment.get_redsys_client(payment.journal)
        request = client.redsys_generate_reversal_request(values)
        data = {
            'Ds_Signature': request['Ds_Signature'],
            'Ds_SignatureVersion': request['Ds_SignatureVersion'],
            'Ds_MerchantParameters': request['Ds_MerchantParameters'],
        }
        try:
            response = requests.post(request['Ds_Redsys_Url'], data=data,
                timeout=60)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self.redsys_error_message = str(e)
            self.redsys_error_code = e.response.status_code
            Refund.fail((self,))
            return '500'

        information = json.loads(response.text)
        try:
            merchant_parameters = payment.get_verified_redsys_data(
                information.get('Ds_MerchantParameters'),
                information.get('Ds_Signature'),
                information.get('Ds_SignatureVersion'), payment.journal)
        except ValueError as exception:
            self.redsys_error_message = str(exception)
            self.redsys_error_code = 'invalid_signature'
            Refund.fail((self,))
            return '500'

        ds_response = merchant_parameters['Ds_Response']

        if ds_response != '0400':
            self.redsys_error_message = merchant_parameters['Ds_Response_Description']
            self.redsys_error_code = ds_response
            Refund.fail((self,))
        else:
            Refund.succeed((self,))
        return self
