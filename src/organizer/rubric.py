"""The rubric editor: /organizer/events/<slug>/rubric.

Gated like every organizer page (`portal_required`, then `get_managed_event`: 404 for anyone who
does not manage this event). All writes go through `scoring.services`.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from events.services import get_managed_event
from scoring import services
from scoring.forms import CriterionTextForm, RubricFormSet, rubric_initial
from scoring.models import Criterion


def _page(request, event, formset=None, error="", status=200):
    criteria = services.with_shares(event.criteria.all())
    for c in criteria:
        c.levels_total = c.max_value - c.min_value + 1
    locked = services.rubric_locked(event)
    if formset is None and not locked:
        formset = RubricFormSet(initial=rubric_initial(criteria), prefix="rubric")
    return render(request, "organizer/rubric.html", {
        "event": event,
        "criteria": criteria,
        "locked": locked,
        "formset": formset,
        "error": error,
    }, status=status)


@never_cache
@portal_required("organizer")
def rubric(request, slug):
    event = get_managed_event(request.user, slug)
    if request.method != "POST":
        return _page(request, event)

    formset = RubricFormSet(request.POST, prefix="rubric")
    if not formset.is_valid():
        return _page(request, event, formset, "some fields are invalid; see the marked rows.", status=400)
    rows = formset.rows()

    if request.POST.get("action") == "split":
        # Fill equal weights into the rows being kept and show them again, unsaved, so the
        # organizer sees exactly what they are about to save.
        kept = [i for i, form in enumerate(formset.forms) if formset.counts(form) and not form.cleaned_data.get("DELETE")]
        data = request.POST.copy()
        for i, weight in zip(kept, services.split_equally(len(kept))):
            data[f"rubric-{i}-weight"] = str(weight.normalize())
        messages.warning(request, "weights split equally. nothing is saved until you press save.")
        return _page(request, event, RubricFormSet(data, prefix="rubric"))

    try:
        changes = services.save_rubric(request, event, rows)
    except services.RubricError as error:
        return _page(request, event, formset, str(error), status=409 if isinstance(error, services.RubricLocked) else 400)
    messages.success(request, f"rubric saved ({len(changes)} change{'s' if len(changes) != 1 else ''}).")
    return redirect("organizer:rubric", slug=event.slug)


@require_POST
@portal_required("organizer")
def rubric_standard(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        services.use_standard_rubric(request, event)
    except services.RubricError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "standard rubric added: functionality, quality, innovation, weighted equally.")
    return redirect("organizer:rubric", slug=event.slug)


@never_cache
@portal_required("organizer")
def criterion_text(request, slug, criterion_id):
    event = get_managed_event(request.user, slug)
    criterion = get_object_or_404(Criterion, pk=criterion_id, event=event)
    form = CriterionTextForm(request.POST or None, criterion=criterion, initial={
        "label": criterion.label, "description": criterion.description,
    })
    if request.method == "POST" and form.is_valid():
        try:
            services.update_criterion_text(
                request, criterion, label=form.cleaned_data["label"],
                description=form.cleaned_data["description"], level_descriptions=form.levels(),
            )
        except services.RubricError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f"'{criterion.label}' saved.")
            return redirect("organizer:rubric", slug=event.slug)
    criterion.share = services.weight_shares(event.criteria.all()).get(criterion.pk)
    return render(request, "organizer/criterion_text.html", {
        "event": event, "criterion": criterion, "form": form,
        "locked": services.rubric_locked(event),
    }, status=400 if request.method == "POST" else 200)
