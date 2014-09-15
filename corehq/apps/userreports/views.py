from django.contrib import messages
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_POST
from corehq.apps.userreports.reports.view import ConfigurableReport
from corehq.apps.userreports.models import ReportConfiguration, IndicatorConfiguration
from corehq.apps.userreports.tasks import rebuild_indicators
from corehq.apps.domain.decorators import domain_admin_required
from corehq.apps.userreports.reports.factory import ReportFactory
from corehq.apps.userreports.ui.forms import ConfigurableReportEditForm, ConfigurableDataSourceEditForm
from corehq.util.couch import get_document_or_404


@domain_admin_required
def edit_report(request, domain, report_id):
    config = get_document_or_404(ReportConfiguration, domain, report_id)
    if request.method == 'POST':
        form = ConfigurableReportEditForm(domain, config, request.POST)
        if form.is_valid():
            config = form.save(commit=False)
            try:
                ReportFactory.from_spec(config)
            except Exception, e:
                messages.error(request, _(u'Problem with report spec: {}').format(e))
            else:
                config.save()
                messages.success(request, _(u'Report {} saved!').format(config.display_name))
                return HttpResponseRedirect(reverse(ConfigurableReport.slug, args=[domain, config._id]))
    else:
        form = ConfigurableReportEditForm(domain, config)
    context = _shared_context(domain)
    context.update({
        'form': form,
    })
    return render(request, "userreports/edit_report_config.html", context)


@domain_admin_required
def edit_data_source(request, domain, config_id):
    config = get_document_or_404(IndicatorConfiguration, domain, config_id)
    if request.method == 'POST':
        form = ConfigurableDataSourceEditForm(config, request.POST)
        if form.is_valid():
            config = form.save()
            try:
                # these two functions will do all the validation we need
                config.filter
                config.indicators
            except Exception, e:
                messages.error(request, _(u'Problem with data source spec: {}').format(e))
            else:
                config.save()
                messages.success(request, _(u'Report {} saved!').format(config.display_name))
    else:
        form = ConfigurableDataSourceEditForm(config)
    context = _shared_context(domain)
    context.update({
        'form': form,
        'data_source': config,
    })
    return render(request, "userreports/edit_data_source.html", context)


@domain_admin_required
@require_POST
def rebuild_data_source(request, domain, config_id):
    config = get_document_or_404(IndicatorConfiguration, domain, config_id)
    messages.success(request,
                     _('Table "{}" is now being rebuilt. Data should start showing up soon'.format(config.display_name)))
    rebuild_indicators.delay(config_id)
    return HttpResponseRedirect(reverse('edit_configurable_data_source', args=[domain, config._id]))


def _shared_context(domain):
    return {
        'domain': domain,
        'reports': ReportConfiguration.by_domain(domain),
        'data_sources': IndicatorConfiguration.by_domain(domain),
    }