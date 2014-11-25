# from django.db import models
from corehq.apps.fixtures.models import FixtureDataItem, FixtureDataType
import requests


class JsonApiError(Exception):
    pass


class Dhis2ApiQueryError(JsonApiError):
    pass


class JsonApiRequest(object):
    """
    Wrap requests with URL, header and authentication for DHIS2 API
    """

    def __init__(self, host, username, password):
        self.baseurl = host + '/api/'
        self.header = {'Accept': 'application/json'}
        self.auth = (username, password)

    @staticmethod
    def json_or_error(response):
        """
        Return HTTP status, JSON

        :raises JsonApiError if HTTP status is not in the 200 (OK) range
        """
        if not 200 <= response.status_code < 300:
            raise JsonApiError('API request failed with HTTP status %s: %s' %
                               (response.status_code, response.text))
        return response.status_code, response.json()

    def get(self, path, **kwargs):
        try:
            response = requests.get(self.baseurl + path, header=self.header, auth=self.auth, **kwargs)
        except Exception as err:  # TODO: What exception?! (socket error; authentication error; ...?)
            raise JsonApiError(str(err))
        return JsonApiRequest.json_or_error(response)

    def post(self, path, data, **kwargs):
        try:
            response = requests.post(self.baseurl + path, data, header=self.header, auth=self.auth, **kwargs)
        except Exception as err:  # TODO: What exception?! (socket error; authentication error; ...?)
            raise JsonApiError(str(err))
        return JsonApiRequest.json_or_error(response)


class FixtureManager(object):
    """
    Reuses the Django manager pattern for fixtures
    """

    def __init__(self, model_class, domain, tag):
        self.model_class = model_class
        self.domain = domain
        self.tag = tag

    def get(self, id_):
        data_type = FixtureDataType.by_domain_tag(self.domain, self.tag).one()
        item = FixtureDataItem.by_field_value(self.domain, data_type, 'id_', id_).one()
        return self.model_class(_fixture_id=item.get_id, **item.fields)

    def all(self):
        for item in FixtureDataItem.get_item_list(self.domain, self.tag):
            yield self.model_class(_fixture_id=item.get_id, **item.fields)


class Dhis2OrgUnit(object):
    """
    Simplify the management of DHIS2 Organisation Units, which are
    stored in a lookup table.
    """

    objects = None  # Manager is set outside of class definition so that we can pass the class to the manager

    def __init__(self, id_, name, _fixture_id=None):
        self.id_ = id_  # param is called "id_" because "id" is a built-in
        self.name = name
        self._fixture_id = _fixture_id

    def save(self):
        data_type = FixtureDataType.by_domain_tag(self.objects.domain, self.objects.tag).one()
        data_item = FixtureDataItem()
        data_item.data_type_id = data_type.get_id
        data_item.domain = self.objects.domain
        data_item.fields = {
            'id_': self.id_,   # Use key "id_" instead of "id" so we have the option of passing as kwargs to __init__
            'name': self.name  # ... which is exactly what FixtureManager does in objects.get() and objects.all()
        }
        data_item.save()
        self._fixture_id = data_item.get_id

    def delete(self):
        if self._fixture_id is None:
            return
        item = FixtureDataItem.get(self._fixture_id)
        item.delete()

Dhis2OrgUnit.objects = FixtureManager(Dhis2OrgUnit, 'dhis2', 'dhis2_org_unit')


def dhis2_entities_to_dicts(json):
    """
    Parse the list of lists returned by a DHIS2 API entity request,
    and return it as a list of dictionaries.

    The DHIS2 API returns entity instances with a list of headers, and
    then a list of lists, as if it was dumping a spreadsheet. e.g. ::

        {
            'headers': [
                {
                    'name': 'instance',
                    'column': 'Instance',
                    'type': 'java.lang.String',
                    'hidden': False,
                    'meta': False
                },
                {
                    'name': 'ou',
                    'column': 'Org unit',
                    'type': 'java.lang.String',
                    'hidden': False,
                    'meta': False
                },
                {
                    'name': 'dv3nChNSIxy',
                    'column': 'First name',
                    'type': 'java.lang.String',
                    'hidden': False,
                    'meta': False
                },
                {
                    'name': 'hwlRTFIFSUq',
                    'column': 'Last name',
                    'type': 'java.lang.String',
                    'hidden': False,
                    'meta': False
                }
            ],
            'rows': [
                [
                    'GpetderUTA7',
                    'Qw7c6Ckb0XC',
                    'Tesmi',
                    'Petros'
                ],
                [
                    'LTxvKtKq48t',
                    'Qw7c6Ckb0XC',
                    'Luwam',
                    'Rezene'
                ],
            ]
        }

    Header "name" values like "dv3nChNSIxy" and "hwlRTFIFSUq" are not
    friendly, and so the returned dictionary uses the "column" value
    as key. The return value for this example would be ::

        [
            {
                'Instance': 'GpetderUTA7',
                'Org unit': 'Qw7c6Ckb0XC',
                'First name: 'Tesmi',
                'Last name': 'Petros'
            },
            {
                'Instance': 'LTxvKtKq48t',
                'Org unit': 'Qw7c6Ckb0XC',
                'First name: 'Luwam',
                'Last name': 'Rezene'
            }
        ]

    The row value of "Tracked entity" will look like "cyl5vuJ5ETQ".
    This isn't very friendly either. But the entity name is given in
    "metaData", which looks like this: ::

        "metaData": {
            "pager": {
                "page": 1,
                "total": 50,
                "pageSize": 50,
                "pageCount": 1
            },
            "names": {
                "cyl5vuJ5ETQ": "Person"
            }
        }

    So we look up tracked entity names, and include them in the dictionary.

    """
    entities = []
    for row in json['rows']:
        entity = {}
        for i, item in enumerate(row):
            if json['headers'][i]['column'] == 'Tracked entity':
                # Look up the name of the tracked entity
                item = json['metaData']['names'][item]
            entity[json['headers'][i]['column']] = item
        entities.append(entity)
    return entities

