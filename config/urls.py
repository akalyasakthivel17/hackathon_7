"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from config_app.time_tracking_views import (
    TimeEntryListCreateView,
    TimeEntryDetailView,
    TimeReportView,
    StartTimerView,
    StopTimerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),

    # Time Tracking APIs
    path("api/time-entries/", TimeEntryListCreateView.as_view(), name="time-entry-list-create"),
    path("api/time-entries/start-timer/", StartTimerView.as_view(), name="start-timer"),
    path("api/time-entries/stop-timer/", StopTimerView.as_view(), name="stop-timer"),
    path("api/time-entries/<str:entry_id>/", TimeEntryDetailView.as_view(), name="time-entry-detail"),
    path("api/time-reports/", TimeReportView.as_view(), name="time-reports"),
]
