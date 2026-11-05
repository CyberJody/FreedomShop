# SPDX-License-Identifier: AGPL-3.0-or-later
"""Map explicit names or the retained QBPOS Customer columns; never split display text."""
NAME_FIELDS = ('title', 'first_name', 'last_name')


def source_names(source_data):
    if not isinstance(source_data, dict) or source_data.get('table') != 'Customer':
        raise ValueError('Missing Customer source evidence.')
    columns = source_data.get('columns', {})
    if not isinstance(columns, dict) or not all(key in columns for key in ('3', '4', '5')):
        raise ValueError('Missing original title, first name, or last name columns.')
    names = {field: columns[column] if columns[column] is not None else ''
             for field, column in zip(NAME_FIELDS, ('3', '4', '5'))}
    if any(not isinstance(value, str) for value in names.values()):
        raise ValueError('Original name fields must be text or null.')
    return names


def composed_name(names):
    return ' '.join(names[field] for field in NAME_FIELDS if names[field])
