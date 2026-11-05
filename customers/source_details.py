# SPDX-License-Identifier: AGPL-3.0-or-later
"""Only explicitly identified Customer columns; never infer unknown bit flags."""
TEXT_COLUMNS={'customer_type':'2','alternate_phone':'25','alternate_phone_2':'26','notes':'28','tax_location':'36'}
BOOL_COLUMNS={'track_as_company':'6','email_opt_in':'52','accept_checks':'55','do_not_mail':'57'}

def source_details(source):
    if not isinstance(source,dict): return {}
    columns=source.get('columns',{}) if source.get('table')=='Customer' else {}
    if not isinstance(columns,dict): columns={}
    result={}
    for field,column in TEXT_COLUMNS.items():
        value=columns.get(column)
        if isinstance(value,str): result[field]=value
    for field,column in BOOL_COLUMNS.items():
        value=columns.get(column)
        if type(value) in (int,bool) and value in (0,1): result[field]=bool(value)
    for field,key in [('alternate_phone','phone3'),('alternate_phone_2','phone4')]:
        if field not in result and isinstance(source.get(key),str): result[field]=source[key]
    return result
