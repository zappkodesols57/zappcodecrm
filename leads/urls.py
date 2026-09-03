from django.urls import path
from . import views

app_name = "leads"

urlpatterns = [
    path("", views.lead_list, name="lead_list"),
    path("add/", views.lead_add, name="lead_add"),
    path("api/check-mobile/", views.check_duplicate_mobile, name="check_duplicate_mobile"),
    path("api/doctor-slots/", views.doctor_slots_api, name="doctor_slots_api"),
    path("<int:pk>/", views.lead_detail, name="lead_detail"),
    path("<int:pk>/edit/", views.lead_edit, name="lead_edit"),
    path("<int:pk>/self-assign/", views.lead_self_assign, name="lead_self_assign"),
    path("<int:pk>/book-appointment/", views.book_appointment, name="book_appointment"),
    path("<int:pk>/assign/", views.assign_lead, name="assign_lead"),
    path("<int:pk>/archive/", views.lead_archive, name="lead_archive"),
    path("<int:pk>/add-note/", views.add_note, name="add_note"),
    path("<int:pk>/add-followup/", views.add_followup, name="add_followup"),
    path("<int:pk>/convert-admission/", views.convert_admission, name="convert_admission"),
    path("bulk-action/", views.bulk_action, name="bulk_action"),
    path("duplicates/", views.duplicates, name="duplicates"),
    path("masters/", views.masters, name="masters"),
    path("course-master/", views.course_master, name="course_master"),
    path("masters/course/<int:pk>/edit/", views.course_edit, name="course_edit"),
    path("masters/<str:kind>/<int:pk>/toggle/", views.master_toggle, name="master_toggle"),

    # Universal Master System routes
    path("universal-masters/", views.universal_master_list, name="universal_masters"),
    path("universal-masters/group/add/", views.master_group_add, name="master_group_add"),
    path("universal-masters/group/<int:pk>/edit/", views.master_group_edit, name="master_group_edit"),
    path("universal-masters/group/<int:pk>/delete/", views.master_group_delete, name="master_group_delete"),
    path("universal-masters/item/add/", views.master_item_add, name="master_item_add"),
    path("universal-masters/item/<int:pk>/edit/", views.master_item_edit, name="master_item_edit"),
    path("universal-masters/item/<int:pk>/toggle/", views.master_item_toggle, name="master_item_toggle"),
    path("universal-masters/item/<int:pk>/delete/", views.master_item_delete, name="master_item_delete"),
    path("universal-masters/import/", views.universal_master_import, name="universal_master_import"),

    # Dynamic Lead Custom Form Fields routes
    path("universal-masters/custom-fields/add/", views.custom_field_add, name="custom_field_add"),
    path("universal-masters/custom-fields/<int:pk>/edit/", views.custom_field_edit, name="custom_field_edit"),
    path("universal-masters/custom-fields/<int:pk>/toggle/", views.custom_field_toggle, name="custom_field_toggle"),
    path("universal-masters/custom-fields/<int:pk>/delete/", views.custom_field_delete, name="custom_field_delete"),

    # Hospital Master Configuration & Cascading Routes
    path("hospital-configuration/", views.hospital_configuration_view, name="hospital_configuration"),
    path("hospital-configuration/profile/save/", views.hospital_profile_save, name="hospital_profile_save"),
    path("hospital-configuration/import/", views.hospital_master_excel_import, name="hospital_master_excel_import"),
    path("hospital-configuration/sample-download/", views.hospital_master_sample_download, name="hospital_master_sample_download"),
    path("hospital-configuration/branch/save/", views.hospital_branch_save, name="hospital_branch_create"),
    path("hospital-configuration/branch/<int:pk>/save/", views.hospital_branch_save, name="hospital_branch_edit"),
    path("hospital-configuration/branch/<int:pk>/toggle/", views.hospital_branch_toggle, name="hospital_branch_toggle"),
    path("hospital-configuration/department/save/", views.hospital_department_save, name="hospital_department_create"),
    path("hospital-configuration/department/<int:pk>/save/", views.hospital_department_save, name="hospital_department_edit"),
    path("hospital-configuration/department/<int:pk>/toggle/", views.hospital_department_toggle, name="hospital_department_toggle"),
    path("hospital-configuration/disease/save/", views.hospital_disease_save, name="hospital_disease_create"),
    path("hospital-configuration/disease/<int:pk>/save/", views.hospital_disease_save, name="hospital_disease_edit"),
    path("hospital-configuration/disease/<int:pk>/toggle/", views.hospital_disease_toggle, name="hospital_disease_toggle"),
    path("hospital-configuration/doctor/save/", views.hospital_doctor_save, name="hospital_doctor_create"),
    path("hospital-configuration/doctor/<int:pk>/save/", views.hospital_doctor_save, name="hospital_doctor_edit"),
    path("hospital-configuration/doctor/<int:pk>/toggle/", views.hospital_doctor_toggle, name="hospital_doctor_toggle"),
    path("api/cascading-hospital-data/", views.cascading_hospital_data_api, name="cascading_hospital_data_api"),
]
