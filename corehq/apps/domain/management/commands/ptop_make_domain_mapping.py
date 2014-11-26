from datetime import datetime
import pprint
from django.core.management.base import NoArgsCommand
import sys
import os
from corehq.apps.domain.models import Domain
from corehq.apps.hqcase.management.commands.ptop_generate_mapping import MappingOutputCommand
from corehq.pillows import dynamic
from corehq.pillows.domain import DomainPillow
from corehq.pillows.dynamic import DEFAULT_MAPPING_WRAPPER, domain_special_types, type_full_date
from django.conf import settings

class Command(MappingOutputCommand):
    help="Generate mapping JSON of our ES indexed types. For domains"
    option_list = NoArgsCommand.option_list + (
    )
    doc_class_str = "corehq.apps.domain.models.Domain"
    doc_class = Domain


    def finish_handle(self):

        filepath = os.path.join(settings.FILEPATH, 'corehq','pillows','mappings','domain_mapping.py')
        domainpillow = DomainPillow(create_index=False)

        #current index
        #check current index
        aliased_indices = domainpillow.check_alias()

        #        current_index = '%s_%s' % (casepillow.es_index_prefix, casepillow.calc_meta())
        current_index = domainpillow.es_index

        #regenerate the mapping dict
        m = DEFAULT_MAPPING_WRAPPER

        init_dict = {
            "cp_n_web_users": {"type": "long"},
            "cp_n_active_cc_users": {"type": "long"},
            "cp_n_cc_users": {"type": "long"},
            "cp_n_users_submitted_form": {"type": "long"},
            "cp_n_60_day_cases": {"type": "long"},
            "cp_n_active_cases": {"type": "long"},
            "cp_n_inactive_cases": {"type": "long"},
            "cp_n_cases": {"type": "long"},
            "cp_n_forms": {"type": "long"},
            "cp_first_form": type_full_date(),
            "cp_last_form": type_full_date(),
            "cp_is_active": {"type": "boolean"},
            "cp_has_app": {"type": "boolean"},
        }

        m['properties'] = dynamic.set_properties(self.doc_class, custom_types=domain_special_types, init_dict=init_dict)
        m['_meta']['comment'] = "Autogenerated [%s] mapping from ptop_generate_mapping %s" % (self.doc_class_str, datetime.utcnow().strftime('%m/%d/%Y'))
        domainpillow.default_mapping = m
        if hasattr(domainpillow, '_calc_meta'):
            delattr(domainpillow, '_calc_meta')
        output = []
        output.append('DOMAIN_INDEX="%s_%s"' % (domainpillow.es_index_prefix, domainpillow.calc_meta()))
        output.append('DOMAIN_MAPPING=%s' % pprint.pformat(m))
        newcalc_index = "%s_%s" % (domainpillow.es_index_prefix, domainpillow.calc_meta())
        print "Writing new domain index and mapping: %s" % output[0]
        with open(filepath, 'w') as outfile:
            outfile.write('\n'.join(output))

        if newcalc_index not in aliased_indices and newcalc_index != current_index:
            sys.stderr.write("\n\tWarning, current index %s is not aliased at the moment\n" % current_index)
            sys.stderr.write("\tCurrent live aliased index: %s\n\n"  % (','.join(aliased_indices)))

        sys.stderr.write("File written to %s\n" % filepath)
