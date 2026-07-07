from django import forms
import json
from .models import (
    Timetable, Constraint, University, Campus, Faculty, Department, 
    Course, Lecturer, StudentGroup, Room, TimeSlot
)

class TimetableForm(forms.ModelForm):
    class Meta:
        model = Timetable
        fields = ['semester', 'name', 'is_active']
        widgets = {
            'semester': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Fall 2026 Schedule V1'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'})
        }

class ConstraintForm(forms.ModelForm):
    parameters_json = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control bg-dark text-white border-secondary',
            'rows': 4,
            'placeholder': 'e.g. {"lecturer_id": 1, "unavailable_slots": [1, 2]}'
        }),
        required=False,
        help_text="Provide parameters as JSON. Leave empty if none are required."
    )

    class Meta:
        model = Constraint
        fields = ['university', 'name', 'constraint_type', 'is_hard', 'weight']
        widgets = {
            'university': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Dr. John unavailable'}),
            'constraint_type': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'is_hard': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'weight': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1})
        }

    def clean_parameters_json(self):
        data = self.cleaned_data.get('parameters_json')
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise forms.ValidationError("Invalid JSON format. Please check syntax.")

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.parameters = self.cleaned_data['parameters_json']
        if commit:
            instance.save()
        return instance

# ==========================================
# RESOURCE MANAGEMENT FORMS
# ==========================================

class UniversityForm(forms.ModelForm):
    class Meta:
        model = University
        fields = ['name', 'code']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Kenyatta University'}),
            'code': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. KU'}),
        }

class CampusForm(forms.ModelForm):
    class Meta:
        model = Campus
        fields = ['university', 'name']
        widgets = {
            'university': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Main Campus'}),
        }

class FacultyForm(forms.ModelForm):
    class Meta:
        model = Faculty
        fields = ['campus', 'name']
        widgets = {
            'campus': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. School of Engineering'}),
        }

class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ['faculty', 'name']
        widgets = {
            'faculty': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Electrical Engineering'}),
        }

class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ['program', 'code', 'name', 'duration_slots', 'sessions_per_week', 'required_room_type', 'lecturer', 'student_group']
        widgets = {
            'program': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'code': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. EE101'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Circuit Theory'}),
            'duration_slots': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1}),
            'sessions_per_week': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1, 'max': 14}),
            'required_room_type': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'lecturer': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'student_group': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
        }

class LecturerForm(forms.ModelForm):
    class Meta:
        model = Lecturer
        fields = ['department', 'name', 'email', 'max_hours_per_week']
        widgets = {
            'department': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Dr. John Smith'}),
            'email': forms.EmailInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. smith@ku.ac.ke'}),
            'max_hours_per_week': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1}),
        }

class StudentGroupForm(forms.ModelForm):
    class Meta:
        model = StudentGroup
        fields = ['program', 'name', 'size']
        widgets = {
            'program': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. EE Year 1'}),
            'size': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1}),
        }

class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ['campus', 'name', 'capacity', 'room_type']
        widgets = {
            'campus': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'e.g. Room A101'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1}),
            'room_type': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
        }

class TimeSlotForm(forms.ModelForm):
    class Meta:
        model = TimeSlot
        fields = ['university', 'day_of_week', 'start_time', 'end_time', 'slot_number', 'is_evening']
        widgets = {
            'university': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'day_of_week': forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'}),
            'start_time': forms.TimeInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'type': 'time'}),
            'slot_number': forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'min': 1}),
            'is_evening': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
