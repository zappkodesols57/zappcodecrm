from django.urls import path
from . import views

app_name = "leads"

urlpatterns = [
    path("", views.lead_list, name="lead_list"),
    path("add/", views.lead_add, name="lead_add"),
    path("api/check-mobile/", views.check_duplicate_mobile, name="check_duplicate_mobile"),
    path("<uuid:pk>/", views.lead_detail, name="lead_detail"),
    path("<uuid:pk>/edit/", views.lead_edit, name="lead_edit"),
    path("<uuid:pk>/assign/", views.assign_lead, name="assign_lead"),
    path("<uuid:pk>/archive/", views.lead_archive, name="lead_archive"),
    path("<uuid:pk>/add-note/", views.add_note, name="add_note"),
    path("<uuid:pk>/add-followup/", views.add_followup, name="add_followup"),
    path("<uuid:pk>/convert-admission/", views.convert_admission, name="convert_admission"),
    path("bulk-action/", views.bulk_action, name="bulk_action"),
    path("duplicates/", views.duplicates, name="duplicates"),
    path("masters/", views.masters, name="masters"),
    path("course-master/", views.course_master, name="course_master"),
    path("masters/course/<uuid:pk>/edit/", views.course_edit, name="course_edit"),
    path("masters/<str:kind>/<uuid:pk>/toggle/", views.master_toggle, name="master_toggle"),

    # Universal Master System routes
    path("universal-masters/", views.universal_master_list, name="universal_masters"),
    path("universal-masters/group/add/", views.master_group_add, name="master_group_add"),
    path("universal-masters/group/<uuid:pk>/edit/", views.master_group_edit, name="master_group_edit"),
    path("universal-masters/group/<uuid:pk>/delete/", views.master_group_delete, name="master_group_delete"),
    path("universal-masters/item/add/", views.master_item_add, name="master_item_add"),
    path("universal-masters/item/<uuid:pk>/edit/", views.master_item_edit, name="master_item_edit"),
    path("universal-masters/item/<uuid:pk>/toggle/", views.master_item_toggle, name="master_item_toggle"),
    path("universal-masters/item/<uuid:pk>/delete/", views.master_item_delete, name="master_item_delete"),
]
