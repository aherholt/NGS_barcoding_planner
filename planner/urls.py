from django.urls import path

from . import views, visual_views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("experiments/", views.experiment_list, name="experiment_list"),
    path("experiments/new/", views.experiment_edit, name="experiment_new"),
    path("experiments/<int:pk>/", views.experiment_detail, name="experiment_detail"),
    path("experiments/<int:pk>/edit/", views.experiment_edit, name="experiment_edit"),
    path("experiments/<int:pk>/upload/", views.experiment_upload, name="experiment_upload"),
    path("experiments/<int:pk>/sample-template/", views.sample_template, name="experiment_sample_template"),
    path("sample-template/", views.sample_template, name="sample_template"),
    path("experiments/<int:pk>/plan/", views.experiment_plan, name="experiment_plan"),
    path("experiments/<int:pk>/history/", views.experiment_history, name="experiment_history"),
    path("experiments/<int:pk>/action/<slug:action>/", views.experiment_action, name="experiment_action"),
    path("experiments/<int:pk>/download/<slug:kind>/", views.experiment_download, name="experiment_download"),
    path("experiments/<int:pk>/plates/", visual_views.experiment_plates, name="experiment_plates"),
    path("experiments/<int:pk>/dnd/", visual_views.experiment_dnd, name="experiment_dnd"),
    path("experiments/<int:pk>/library/", visual_views.experiment_library, name="experiment_library"),
    path("experiments/<int:pk>/library/<slug:action>/", visual_views.experiment_library_api, name="experiment_library_api"),
    path("runs/", views.run_list, name="run_list"),
    path("runs/new/", views.run_edit, name="run_new"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/edit/", views.run_edit, name="run_edit"),
    path("runs/<int:pk>/indexes/", visual_views.run_indexes, name="run_indexes"),
    path("runs/<int:pk>/dnd/", visual_views.run_dnd, name="run_dnd"),
    path("runs/<int:pk>/action/<slug:action>/", views.run_action, name="run_action"),
    path("runs/<int:pk>/download/<slug:kind>/", views.run_download, name="run_download"),
]
