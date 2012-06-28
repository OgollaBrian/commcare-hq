from datetime import timedelta, datetime
import json
from copy import deepcopy
from django.core.files import temp
from django.http import HttpResponseRedirect, HttpResponse
from django.core.urlresolvers import reverse
from django.contrib.auth.decorators import permission_required
from django.template.context import RequestContext
from corehq.apps.builds.models import CommCareBuildConfig, BuildSpec
from corehq.apps.domain.models import Domain
from corehq.apps.reports.datatables import DataTablesColumn, DataTablesHeader, DTSortType
from corehq.apps.sms.models import SMSLog
from corehq.apps.users.models import CouchUser, CommCareUser
from couchforms.models import XFormInstance
from dimagi.utils.couch.database import get_db
from collections import defaultdict
from corehq.apps.domain.decorators import login_and_domain_required, require_superuser
from dimagi.utils.decorators.datespan import datespan_in_request
from dimagi.utils.parsing import json_format_datetime, string_to_datetime
from dimagi.utils.web import json_response, render_to_response
from django.views.decorators.cache import cache_page
from couchexport.export import export_raw, export_from_tables
from couchexport.shortcuts import export_response
from couchexport.models import Format
from StringIO import StringIO
from django.template.defaultfilters import yesno
from dimagi.utils.excel import WorkbookJSONReader
from dimagi.utils.decorators.view import get_file
from django.contrib import messages

@require_superuser
def default(request):
    return HttpResponseRedirect(reverse("domain_list"))

datespan_default = datespan_in_request(
    from_param="startdate",
    to_param="enddate",
    default_days=30,
)


def get_hqadmin_base_context(request):
    try:
        domain = request.user.selected_domain.name
    except AttributeError:
        domain = None

    return {
        "domain": domain,
    }

def _all_domain_stats():
    webuser_counts = defaultdict(lambda: 0)
    commcare_counts = defaultdict(lambda: 0)
    form_counts = defaultdict(lambda: 0)
    case_counts = defaultdict(lambda: 0)
    
    for row in get_db().view('users/by_domain', startkey=["active"], 
                             endkey=["active", {}], group_level=3).all():
        _, domain, doc_type = row['key']
        value = row['value']
        {
            'WebUser': webuser_counts,
            'CommCareUser': commcare_counts
        }[doc_type][domain] = value

    form_counts.update(dict([(row["key"][0], row["value"]) for row in \
                             get_db().view("reports/all_submissions", 
                                           group=True,group_level=1).all()]))
    
    case_counts.update(dict([(row["key"][0], row["value"]) for row in \
                             get_db().view("hqcase/types_by_domain", 
                                           group=True,group_level=1).all()]))
    
    return {"web_users": webuser_counts, 
            "commcare_users": commcare_counts,
            "forms": form_counts,
            "cases": case_counts}

@require_superuser
def domain_list(request):
    # one wonders if this will eventually have to paginate
    domains = Domain.get_all()
    all_stats = _all_domain_stats()
    for dom in domains:
        dom.web_users = int(all_stats["web_users"][dom.name])
        dom.commcare_users = int(all_stats["commcare_users"][dom.name])
        dom.cases = int(all_stats["cases"][dom.name])
        dom.forms = int(all_stats["forms"][dom.name])
        dom.admins = [row["doc"]["email"] for row in get_db().view("users/admins_by_domain", key=dom.name, reduce=False, include_docs=True).all()]

    context = get_hqadmin_base_context(request)
    context.update({"domains": domains})
    context['layout_flush_content'] = True

    headers = DataTablesHeader(
        DataTablesColumn("Domain"),
        DataTablesColumn("# Web Users", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("# Mobile Workers", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("# Cases", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("# Submitted Forms", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("Domain Admins")
    )
    context["headers"] = headers
    context["aoColumns"] = headers.render_aoColumns
    return render_to_response(request, "hqadmin/domain_list.html", context)

@require_superuser
def active_users(request):
    keys = []
    number_threshold = 15
    date_threshold_days_ago = 90
    date_threshold = json_format_datetime(datetime.utcnow() - timedelta(days=date_threshold_days_ago))
    for line in get_db().view("reports/submit_history", group_level=2):
        if line['value'] >= number_threshold:
            keys.append(line["key"])

    final_count = defaultdict(int)

    def is_valid_user_id(user_id):
        if not user_id: return False
        try:
            get_db().get(user_id)
            return True
        except Exception:
            return False

    for domain, user_id in keys:
        if get_db().view("reports/submit_history", reduce=False, startkey=[domain, user_id, date_threshold], limit=1):
            if True or is_valid_user_id(user_id):
                final_count[domain] += 1

    return json_response({"break_down": final_count, "total": sum(final_count.values())})

@require_superuser
def global_report(request, template="hqadmin/global.html", as_export=False):

    def _flot_format(result):
        return int(datetime(year=result['key'][0], month=result['key'][1], day=1).strftime("%s"))*1000

    def _export_format(result):
        return datetime(year=result['key'][0], month=result['key'][1], day=1).strftime("%Y-%m-%d")

    context = get_hqadmin_base_context(request)

    def _metric(name):
        counts = []
        for result in get_db().view("hqadmin/%ss_over_time" % name, group_level=2):
            if not result or not result.has_key('key') or not result.has_key('value'): continue
            if result['key'][0] and int(result['key'][0]) >= 2010 and \
               (int(result['key'][0]) < datetime.utcnow().year or
                (int(result['key'][0]) == datetime.utcnow().year and
                 int(result['key'][1]) <= datetime.utcnow().month)):
                counts.append([_export_format(result) if as_export else _flot_format(result), result['value']])
        context['%s_counts' % name] = counts
        counts_int = deepcopy(counts)
        for i in range(1, len(counts_int)):
            if isinstance(counts_int[i][1], int):
                counts_int[i][1] += counts_int[i-1][1]
        context['%s_counts_int' % name] = counts_int

    standard_metrics = ["case", "form", "user"]
    for m in standard_metrics:
        _metric(m)

    def _active_metric(name):
        dates = {}
        for result in get_db().view("hqadmin/%ss_over_time" % name, group=True):
            if not result or not result.has_key('key') or not result.has_key('value'): continue
            if result['key'][0] and int(result['key'][0]) >= 2010 and\
               (int(result['key'][0]) < datetime.utcnow().year or
                (int(result['key'][0]) == datetime.utcnow().year and
                 int(result['key'][1]) <= datetime.utcnow().month)):
                date = _export_format(result) if as_export else _flot_format(result)
                if not date in dates:
                    dates[date] = set([result['key'][2]])
                else:
                    dates[date].update([result['key'][2]])
        datelist = [[date, dates[date]] for date in sorted(dates.keys())]
        domainlist = [[x[0], len(x[1])] for x in datelist]
        domainlist_int = deepcopy(datelist)
        for i in range(1, len(domainlist_int)):
            domainlist_int[i][1] = list(set(domainlist_int[i-1][1]).union(domainlist_int[i][1]))
        domainlist_int = [[x[0], len(x[1])] for x in domainlist_int]
        context['%s_counts' % name] = domainlist
        context['%s_counts_int' % name] = domainlist_int

    active_metrics = ["active_domain", "active_user"]
    for a in active_metrics:
        _active_metric(a)


    if as_export:
        all_metrics = standard_metrics + active_metrics
        format = request.GET.get("format", "xls")
        tables = []
        for metric_name in all_metrics:
            table = context.get('%s_counts' % metric_name, [])
            table = [["%s" % item[0], "%d" % item[1]] for item in table]
            table.reverse()
            table.append(["date", "%s count" % metric_name])
            table.reverse()

            table_int = context.get('%s_counts_int' % metric_name, [])
            table_int = [["%s" % item[0], "%d" % item[1]] for item in table_int]
            table_int.reverse()
            table_int.append(["date", "%s count, cumulative" % metric_name])
            table_int.reverse()

            tables.append(["%s counts" % metric_name, table])
            tables.append(["%s cumulative" % metric_name, table_int])
        temp = StringIO()
        export_from_tables(tables, temp, format)
        return export_response(temp, format, "GlobalReport")

    context['hide_filters'] = True

    return render_to_response(request, template, context)

@require_superuser
def commcare_version_report(request, template="hqadmin/commcare_version.html"):
    apps = get_db().view('app_manager/applications_brief').all()
    menu = CommCareBuildConfig.fetch().menu
    builds = [item.build.to_string() for item in menu]
    by_build = dict([(item.build.to_string(), {"label": item.label, "apps": []}) for item in menu])

    for app in apps:
        app = app['value']
        app['id'] = app['_id']
        if app.get('build_spec'):
            build_spec = BuildSpec.wrap(app['build_spec'])
            build = build_spec.to_string()
            if by_build.has_key(build):
                by_build[build]['apps'].append(app)
            else:
                by_build[build] = {"label": build_spec.get_label(), "apps": [app]}
                builds.append(build)

    tables = []
    for build in builds:
        by_build[build]['build'] = build
        tables.append(by_build[build])
    context = get_hqadmin_base_context(request)
    context.update({'tables': tables})
    context['hide_filters'] = True
    return render_to_response(request, template, context)


@cache_page(60*5)
def _cacheable_domain_activity_report(request):
    landmarks = json.loads(request.GET.get('landmarks') or "[7, 30, 90]")
    landmarks.sort()
    now = datetime.utcnow()
    dates = []
    for landmark in landmarks:
        dates.append(now - timedelta(days=landmark))

    domains = [{'name': domain.name} for domain in Domain.get_all()]

    for domain in domains:
        domain['users'] = dict([(user.user_id, {'raw_username': user.raw_username}) for user in CommCareUser.by_domain(domain['name'])])
        if not domain['users']:
            continue
        forms = [r['value'] for r in get_db().view('reports/all_submissions',
            reduce=False,
            startkey=[domain['name'], json_format_datetime(dates[-1])],
            endkey=[domain['name'], json_format_datetime(now)],
        ).all()]
        domain['user_sets'] = [dict() for landmark in landmarks]

        for form in forms:
            user_id = form.get('user_id')
            time = string_to_datetime(form['time']).replace(tzinfo = None)
            if user_id in domain['users']:
                for i, date in enumerate(dates):
                    if time > date:
                        domain['user_sets'][i][user_id] = domain['users'][user_id]

    return HttpResponse(json.dumps({'domains': domains, 'landmarks': landmarks}))

@require_superuser
def domain_activity_report(request, template="hqadmin/domain_activity_report.html"):
    context = get_hqadmin_base_context(request)
    context.update(json.loads(_cacheable_domain_activity_report(request).content))

    context['layout_flush_content'] = True
    headers = DataTablesHeader(
        DataTablesColumn("Domain")
    )
    for landmark in context['landmarks']:
        headers.add_column(DataTablesColumn("Last %s Days" % landmark))
    headers.add_column(DataTablesColumn("All Users"))
    context["headers"] = headers
    context["aoColumns"] = headers.render_aoColumns
    return render_to_response(request, template, context)

@datespan_default
@require_superuser
def message_log_report(request):
    show_dates = True
    
    datespan = request.datespan
    domains = Domain.get_all()
    for dom in domains:
        dom.sms_incoming = SMSLog.count_incoming_by_domain(dom.name, datespan.startdate_param, datespan.enddate_param)
        dom.sms_outgoing = SMSLog.count_outgoing_by_domain(dom.name, datespan.startdate_param, datespan.enddate_param)
        dom.sms_total = SMSLog.count_by_domain(dom.name, datespan.startdate_param, datespan.enddate_param)

    context = get_hqadmin_base_context(request)

    headers = DataTablesHeader(
        DataTablesColumn("Domain"),
        DataTablesColumn("Incoming Messages", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("Outgoing Messages", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("Total Messages", sort_type=DTSortType.NUMERIC)
    )
    context["headers"] = headers
    context["aoColumns"] = headers.render_aoColumns

    context.update({
        "domains": domains,
        "show_dates": show_dates,
        "datespan": datespan
    })

    context['layout_flush_content'] = True
    return render_to_response(request, "hqadmin/message_log_report.html", context)

def _get_emails():
    return [r['key'] for r in get_db().view('hqadmin/emails').all()]

@require_superuser
def emails(request):
    email_list = _get_emails()
    return HttpResponse('"' + '", "'.join(email_list) + '"')

@datespan_default
@require_superuser
def submissions_errors(request, template="hqadmin/submissions_errors_report.html"):
    show_dates = "true"
    datespan = request.datespan
    domains = Domain.get_all()

    rows = []
    for domain in domains:
        key = ["active", domain.name]
        data = get_db().view('users/by_domain',
            startkey=key,
            endkey=key+[{}],
            reduce=True
        ).all()
        num_active_users = data[0].get('value', 0) if data else 0

        key = [domain.name]
        data = get_db().view('reports/all_submissions',
            startkey=key+[datespan.startdate_param_utc],
            endkey=key+[datespan.enddate_param_utc, {}],
            reduce=True
        ).all()
        num_forms_submitted = data[0].get('value', 0) if data else 0

        key = [domain.name, "all_errors_only"]
        data = get_db().view("phonelog/devicelog_data",
            reduce=True,
            startkey=key+[datespan.startdate_param_utc],
            endkey=key+[datespan.enddate_param_utc]
        ).first()
        num_errors = 0
        num_warnings = 0
        if data:
            data = data.get('value', {})
            num_errors = data.get('errors', 0)
            num_warnings = data.get('warnings', 0)

        rows.append(dict(domain=domain.name,
                        active_users=num_active_users,
                        submissions=num_forms_submitted,
                        errors=num_errors,
                        warnings=num_warnings))

    context = get_hqadmin_base_context(request)
    context.update({
        "show_dates": show_dates,
        "datespan": datespan,
        "layout_flush_content": True,
        "rows": rows
    })

    headers = DataTablesHeader(
        DataTablesColumn("Domain"),
        DataTablesColumn("Active Users", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("Forms Submitted", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("Errors", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("Warnings", sort_type=DTSortType.NUMERIC)
    )
    context["headers"] = headers
    context["aoColumns"] = headers.render_aoColumns

    return render_to_response(request, template, context)

@require_superuser
@get_file("file")
def update_domains(request):
    if request.method == "POST":
        try:
            workbook = WorkbookJSONReader(request.file)
            domains = workbook.get_worksheet(title='domains')
            success_count = 0
            fail_count = 0
            for row in domains:
                try:
                    name = row["name"]
                    domain = Domain.get_by_name(name)
                    if domain:
                        for k, v in row.items():
                            setattr(domain, k, v)
                        domain.save()
                        success_count += 1
                    else:
                        messages.warning(request, "No domain with name %s found" % name)
                        fail_count += 1
                except Exception, e:
                    messages.warning("Update for %s failed: %s" % e)
                    fail_count += 1
            if success_count:
                messages.success(request, "%s domains successfully updated" % success_count)
            if fail_count:
                messages.error(request, "%s domains had errors. details above." % fail_count)
            
        except Exception, e:
            messages.error(request, "Something went wrong! Update failed. Here's your error: %s" % e)
            
    # one wonders if this will eventually have to paginate
    domains = Domain.get_all()
    all_stats = _all_domain_stats()
    for dom in domains:
        dom.web_users = int(all_stats["web_users"][dom.name])
        dom.commcare_users = int(all_stats["commcare_users"][dom.name])
        dom.cases = int(all_stats["cases"][dom.name])
        dom.forms = int(all_stats["forms"][dom.name])
        if dom.forms:
            try:
                dom.first_submission = string_to_datetime(XFormInstance.get_db().view\
                    ("receiverwrapper/all_submissions_by_domain", 
                     reduce=False, limit=1, 
                     startkey=[dom.name, "by_date"],
                     endkey=[dom.name, "by_date", {}]).all()[0]["key"][2]).strftime("%Y-%m-%d")
            except ValueError:
                dom.first_submission = ""
            
            try:
                dom.last_submission = string_to_datetime(XFormInstance.get_db().view\
                    ("receiverwrapper/all_submissions_by_domain", 
                     reduce=False, limit=1, descending=True,
                     startkey=[dom.name, "by_date", {}],
                     endkey=[dom.name, "by_date"]).all()[0]["key"][2]).strftime("%Y-%m-%d")
            except ValueError:
                dom.last_submission = ""
        else:
            dom.first_submission = ""
            dom.last_submission = ""
            
        
    context = get_hqadmin_base_context(request)
    context.update({"domains": domains})
    
    headers = DataTablesHeader(
        DataTablesColumn("Domain"),
        DataTablesColumn("City"),
        DataTablesColumn("Country"),
        DataTablesColumn("Region"),
        DataTablesColumn("Project Type"),
        DataTablesColumn("Customer Type"),
        DataTablesColumn("Is Test"),
        DataTablesColumn("# Web Users", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("# Mobile Workers", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("# Cases", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("# Submitted Forms", sort_type=DTSortType.NUMERIC),
        DataTablesColumn("First Submission"),
        DataTablesColumn("Most Recent Submission"),
        DataTablesColumn("Edit")
    )
    context["headers"] = headers
    context["aoColumns"] = headers.render_aoColumns
    return render_to_response(request, "hqadmin/domain_update_properties.html", context)

@require_superuser
def domain_list_download(request):
    domains = Domain.get_all()
    properties = ("name", "city", "country", "region", "project_type", 
                  "customer_type", "is_test?")
    
    def _row(domain):
        def _prop(domain, prop):
            if prop.endswith("?"):
                return yesno(getattr(domain, prop[:-1]))
            return getattr(domain, prop) or ""
        return (_prop(domain, prop) for prop in properties)
    
    temp = StringIO()
    headers = (("domains", properties),)   
    data = (("domains", (_row(domain) for domain in domains)),)
    export_raw(headers, data, temp)
    return export_response(temp, Format.XLS_2007, "domains")
