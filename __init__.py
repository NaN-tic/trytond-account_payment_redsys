# This file is part account_payment_redsys module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import payment

def register():
    Pool.register(
        payment.PaymentJournal,
        payment.PaymentGroup,
        payment.Payment,
        payment.Account,
        payment.AccountAccount,
        module='account_payment_redsys', type_='model')
