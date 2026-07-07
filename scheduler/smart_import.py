import re
import datetime
from django.db import transaction
from scheduler.models import (
    University, Campus, Faculty, Department, Program, Semester,
    Lecturer, StudentGroup, Room, Course, TimeSlot, Timetable
)

COLUMN_ALIASES = {
    'lecturer': [
        'lecturer', 'instructor', 'teacher', 'tutor', 'facilitator',
        'professor', 'staff', 'academic', 'taught_by', 'faculty_name',
        'staff_name', 'lecturer_name', 'instructor_name', 'prof',
        'teaching_staff', 'academic_staff', 'teacher_name', 'name',
    ],
    'course_code': [
        'unit_code', 'course_code', 'module_code', 'subject_code',
        'code', 'course_id', 'module_id', 'unit_id', 'subject_id',
        'course_no', 'unit_no', 'class_code',
    ],
    'course_name': [
        'unit_name', 'course_name', 'module_name', 'subject_name',
        'unit', 'course', 'module', 'subject', 'course_title',
        'unit_title', 'module_title', 'class_name', 'class_title', 'name',
    ],
    'room': [
        'room', 'venue', 'hall', 'location', 'classroom', 'class_room',
        'lecture_room', 'lab', 'room_name', 'venue_name', 'facility',
        'teaching_room', 'room_no', 'room_number', 'space', 'name',
        'r', 'rm',
    ],
    'day': [
        'day', 'day_of_week', 'weekday', 'day#', 'day_no', 'day_num',
    ],
    'time': [
        'time', 'time_slot', 'period', 'schedule_time', 'hours',
        'start_time', 'session_time', 'class_time', 'timing',
    ],
    'program': [
        'program', 'programme', 'course_of_study', 'degree',
        'program_name', 'programme_name', 'stream', 'major',
    ],
    'campus': [
        'campus', 'campus_name', 'site', 'branch', 'centre', 'center',
        'location', 'school',
    ],
    'mode': [
        'mode', 'study_mode', 'mode_of_study', 'learning_mode',
        'delivery_mode', 'session_type',
    ],
    'semester': [
        'trimester', 'semester', 'term', 'academic_period', 'period',
        'year_of_study', 'level', 'year', 'intake',
    ],
    'capacity': [
        'capacity', 'seats', 'size', 'count', 'enrollment', 'enrolment',
        'students', 'no_of_students', 'max_students', 'class_size',
    ],
    'email': [
        'email', 'email_address', 'e_mail', 'mail', 'lecturer_email',
        'staff_email', 'instructor_email',
    ],
    'department': [
        'department', 'dept', 'department_name', 'dept_name',
        'faculty', 'school', 'division',
    ],
    'room_type': [
        'room_type', 'venue_type', 'type', 'facility_type',
        'required_room_type', 'space_type',
    ],
    'hours': [
        'hours', 'duration', 'duration_hours', 'credit_hours',
        'contact_hours', 'duration_slots', 'num_hours',
    ],
    'student_group': [
        'student_group', 'group', 'class', 'section', 'cohort',
        'group_name', 'class_name', 'student_group_name', 'batch',
    ],
    'remarks': [
        'remarks', 'remark', 'notes', 'note', 'comment', 'comments',
        'observation', 'additional_info',
    ],
    'option': [
        'option', 'specialisation', 'specialization', 'major_option',
    ],
}

DAY_NAME_MAP = {
    'monday': 1, 'mon': 1, 'm': 1, '1': 1,
    'tuesday': 2, 'tue': 2, 't': 2, '2': 2,
    'wednesday': 3, 'wed': 3, 'w': 3, '3': 3,
    'thursday': 4, 'thu': 4, 'th': 4, '4': 4,
    'friday': 5, 'fri': 5, 'f': 5, '5': 5,
    'saturday': 6, 'sat': 6, 's': 6, '6': 6,
    'sunday': 7, 'sun': 7, 'su': 7, '7': 7
}

def normalize_string(val):
    if val is None:
        return ""
    return re.sub(r'[\s_\-]+', '_', str(val).strip().lower())

def map_headers(headers):
    """
    Given list of raw headers, maps to system keys based on aliases.
    """
    mapped = {}
    for idx, hdr in enumerate(headers):
        if hdr is None:
            continue
        norm = normalize_string(hdr)
        
        # Exact match / alias match
        for sys_key, aliases in COLUMN_ALIASES.items():
            if norm in aliases or normalize_string(hdr) in [normalize_string(a) for a in aliases]:
                # Map only if not already mapped
                if sys_key not in mapped:
                    mapped[sys_key] = idx
                    break
    return mapped

def slugify_name(name):
    # Remove titles
    name_clean = re.sub(r'^(dr|prof|mr|mrs|ms|sir|madam)\b\.?\s*', '', name, flags=re.IGNORECASE)
    # Replace non-alphanumeric with a dot
    slug = re.sub(r'[^a-zA-Z0-9]+', '.', name_clean.strip().lower())
    return slug.strip('.')

def normalize_course_code(code):
    if not code:
        return ""
    code_clean = re.sub(r'\s+', '', str(code)).upper()
    match = re.match(r'^([A-Z]{3,4})(\d{4}[A-Z]?)$', code_clean)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return code_clean

def generate_lecturer_email(name, university_name):
    slug = slugify_name(name)
    uni_slug = re.sub(r'[^a-zA-Z0-9]+', '', university_name.lower())
    if not uni_slug:
        uni_slug = "university"
    return f"{slug}@{uni_slug}.edu"

def parse_day(val):
    if val is None:
        return None
    val_norm = str(val).strip().lower()
    return DAY_NAME_MAP.get(val_norm)

def parse_time_slot(time_str):
    if not time_str:
        return None, None
    time_str = str(time_str).strip().upper()
    time_str = re.sub(r'\s+TO\s+|\s+AND\s+|\s*-\s*', '-', time_str)
    time_str = time_str.replace('HRS', '').replace('HR', '').strip()
    
    parts = time_str.split('-')
    if len(parts) != 2:
        return None, None
    
    start_raw = parts[0].strip()
    end_raw = parts[1].strip()
    
    def parse_part(raw):
        raw = raw.strip()
        is_pm = False
        is_am = False
        if 'PM' in raw:
            is_pm = True
            raw = raw.replace('PM', '').strip()
        elif 'AM' in raw:
            is_am = True
            raw = raw.replace('AM', '').strip()
            
        if ':' in raw:
            time_parts = raw.split(':')
            hour = int(time_parts[0])
            minute = int(time_parts[1])
        else:
            if len(raw) == 4 and raw.isdigit():
                hour = int(raw[:2])
                minute = int(raw[2:])
            elif len(raw) <= 2 and raw.isdigit():
                hour = int(raw)
                minute = 0
            else:
                return None
                
        if is_pm and hour < 12:
            hour += 12
        if is_am and hour == 12:
            hour = 0
            
        return datetime.time(hour=hour, minute=minute)
        
    try:
        start_time = parse_part(start_raw)
        end_time = parse_part(end_raw)
        return start_time, end_time
    except Exception:
        return None, None

def detect_format(workbook) -> dict:
    sheet_infos = []
    
    # Sheet name keywords for mapping
    ENTITY_SHEET_KEYWORDS = {
        'room': ['room', 'venue', 'location', 'hall'],
        'lecturer': ['lecturer', 'instructor', 'teacher', 'staff'],
        'student_group': ['student', 'group', 'class', 'cohort', 'section'],
        'course': ['course', 'unit', 'module', 'subject'],
    }
    
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        
        # Find headers (first non-empty row)
        headers = []
        header_row_idx = 0
        for idx, r in enumerate(rows):
            if any(r):
                headers = [str(x).strip() if x is not None else '' for x in r]
                header_row_idx = idx
                break
                
        if not headers:
            continue
            
        column_map = map_headers(headers)
        
        # Analyze sheet name
        sheet_name_norm = sheet.title.strip().lower()
        detected_type = 'unknown'
        
        # Check flat timetable indicators: contains day + time + room + lecturer + course
        has_day = 'day' in column_map
        has_time = 'time' in column_map
        has_room = 'room' in column_map
        has_lecturer = 'lecturer' in column_map
        has_course = 'course_code' in column_map or 'course_name' in column_map
        
        if has_day and has_time and has_room and has_lecturer and has_course:
            detected_type = 'flat_timetable'
        else:
            # Check sheet keywords
            matched_type = None
            for key, keywords in ENTITY_SHEET_KEYWORDS.items():
                if any(kw in sheet_name_norm for kw in keywords):
                    matched_type = key
                    break
            
            if matched_type:
                # Confirm we have at least one key column mapped for that type
                if matched_type == 'room' and ('room' in column_map or 'room_type' in column_map):
                    detected_type = 'room'
                elif matched_type == 'lecturer' and ('lecturer' in column_map or 'email' in column_map):
                    detected_type = 'lecturer'
                elif matched_type == 'student_group' and ('student_group' in column_map or 'capacity' in column_map):
                    detected_type = 'student_group'
                elif matched_type == 'course' and ('course_code' in column_map or 'course_name' in column_map):
                    detected_type = 'course'
            
            # Fallback based on column matching
            if detected_type == 'unknown':
                if has_day and has_time:
                    detected_type = 'flat_timetable'
                elif 'room' in column_map and 'capacity' in column_map:
                    detected_type = 'room'
                elif 'lecturer' in column_map and 'email' in column_map:
                    detected_type = 'lecturer'
                elif 'student_group' in column_map and 'capacity' in column_map:
                    detected_type = 'student_group'
                elif 'course_code' in column_map:
                    detected_type = 'course'
                    
        sheet_infos.append({
            'name': sheet.title,
            'detected_type': detected_type,
            'column_map': column_map,
            'headers': headers,
            'header_row_idx': header_row_idx,
            'mappings': [{'header': headers[idx], 'sys_key': sys_key} for sys_key, idx in column_map.items()]
        })
        
    # Classify overall type
    total_sheets = len(sheet_infos)
    timetable_sheets = [s for s in sheet_infos if s['detected_type'] == 'flat_timetable']
    room_sheets = [s for s in sheet_infos if s['detected_type'] == 'room']
    lecturer_sheets = [s for s in sheet_infos if s['detected_type'] == 'lecturer']
    course_sheets = [s for s in sheet_infos if s['detected_type'] == 'course']
    group_sheets = [s for s in sheet_infos if s['detected_type'] == 'student_group']
    
    if len(timetable_sheets) > 0:
        overall_type = 'flat_timetable'
        confidence = len(timetable_sheets) / total_sheets
    elif len(room_sheets) > 0 or len(lecturer_sheets) > 0 or len(course_sheets) > 0 or len(group_sheets) > 0:
        overall_type = 'separate_entities'
        confidence = (len(room_sheets) + len(lecturer_sheets) + len(course_sheets) + len(group_sheets)) / total_sheets
    else:
        overall_type = 'single_sheet'
        confidence = 0.5
        
    return {
        'type': overall_type,
        'sheets': sheet_infos,
        'confidence': round(confidence, 2)
    }

def extract_entities(workbook, format_info, university) -> dict:
    """
    Extracts all entities from the workbook based on format_info and normalizes data.
    """
    campuses = {}
    programs = {}
    lecturers = {}
    rooms = {}
    student_groups = {}
    courses = {}
    time_slots = {}
    warnings = []
    
    uni_name = university.name

    program_casing_map = {}
    def canonical_program_name(name):
        if not name:
            return "Default Program"
        name_clean = re.sub(r'\s+', ' ', str(name).strip())
        key = name_clean.lower()
        if key not in program_casing_map:
            words = name_clean.split(' ')
            if words:
                first_word = words[0].upper()
                if first_word in ['PHD', 'MBA', 'BSC', 'BCOM', 'BPL', 'CBM', 'CPL', 'DBM', 'DPL', 'IBM', 'BPM', 'IT', 'CS']:
                    words[0] = first_word
                    name_clean = ' '.join(words)
            program_casing_map[key] = name_clean
        return program_casing_map[key]

    campus_casing_map = {}
    def canonical_campus_name(name):
        if not name:
            return "Main Campus"
        name_clean = re.sub(r'\s+', ' ', str(name).strip())
        key = name_clean.lower()
        if key not in campus_casing_map:
            campus_casing_map[key] = name_clean
        return campus_casing_map[key]
    
    # Helper to clean room type
    def clean_room_type(name, raw_type):
        name_lower = str(name or '').lower()
        type_lower = str(raw_type or '').lower()
        
        # Check explicit type
        if 'lab' in type_lower or 'laboratory' in type_lower:
            return 'Lab'
        if 'lecture' in type_lower or 'hall' in type_lower or 'theater' in type_lower or 'theatre' in type_lower:
            return 'Lecture'
        if 'seminar' in type_lower or 'tutorial' in type_lower or 'meeting' in type_lower:
            return 'Seminar'
            
        # Infer from name
        if 'lab' in name_lower or 'laboratory' in name_lower or 'workshop' in name_lower or 'studio' in name_lower:
            return 'Lab'
        if 'lh' in name_lower or 'hall' in name_lower or 'theatre' in name_lower or 'theater' in name_lower or 'auditorium' in name_lower:
            return 'Lecture'
        if 'seminar' in name_lower or 'tutorial' in name_lower or 'room' in name_lower:
            return 'Seminar'
            
        return 'Lecture' # default fallback

    # Process each sheet
    for sheet_info in format_info['sheets']:
        sheet_name = sheet_info['name']
        dtype = sheet_info['detected_type']
        cmap = sheet_info['column_map']
        
        sheet = workbook[sheet_name]
        rows = list(sheet.iter_rows(values_only=True))
        if len(rows) <= sheet_info['header_row_idx'] + 1:
            continue
            
        data_rows = rows[sheet_info['header_row_idx'] + 1:]
        
        # Helper to get safe value using column mapping
        def clean_val(val, default=''):
            if val is None:
                return default
            import datetime
            if isinstance(val, (datetime.datetime, datetime.date)):
                return f"{val.month}-{val.day}"
            if isinstance(val, (int, float)):
                if 40000 <= val <= 50000:
                    dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(val))
                    return f"{dt.month}-{dt.day}"
                if isinstance(val, float) and val.is_integer():
                    return str(int(val))
                return str(val)
            val_str = str(val).strip()
            import re
            if re.match(r'^\d{4}-\d{2}-\d{2}', val_str):
                try:
                    dt = datetime.datetime.strptime(val_str.split()[0], '%Y-%m-%d')
                    return f"{dt.month}-{dt.day}"
                except:
                    pass
            if re.match(r'^\d{5}(\.0)?$', val_str):
                try:
                    num = int(float(val_str))
                    if 40000 <= num <= 50000:
                        dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=num)
                        return f"{dt.month}-{dt.day}"
                except:
                    pass
            return val_str

        def get_val(row, key, default=''):
            if key not in cmap:
                return default
            idx = cmap[key]
            if idx >= len(row):
                return default
            return clean_val(row[idx], default)
            
        if dtype == 'flat_timetable' or (format_info['type'] == 'flat_timetable' and dtype == 'unknown'):
            # In flat timetable, each row has day, time, room, course, lecturer, group info
            for idx, r in enumerate(data_rows, sheet_info['header_row_idx'] + 2):
                if not any(r):
                    continue
                    
                # 1. Parse Course info (Code + Name)
                c_code_raw = get_val(r, 'course_code')
                if normalize_string(c_code_raw) in ('unit_code', 'course_code', 'code', 'day', 'day_no', 'day_num', 'unitcode', 'coursecode'):
                    continue
                if str(c_code_raw).strip().upper() in ('UNIT CODE', 'UNIT_CODE', 'CODE', 'COURSE_CODE', 'COURSE CODE', 'DAY#', 'DAY'):
                    continue
                c_code = normalize_course_code(c_code_raw)
                c_name = get_val(r, 'course_name')
                if c_name:
                    c_name = c_name.strip()
                
                # If only one is found, make them match
                if not c_code and not c_name:
                    continue # Need course
                if not c_code:
                    c_code = normalize_course_code(re.sub(r'\s+', '', c_name)[:8])
                if not c_name:
                    c_name = c_code
                
                c_code = normalize_course_code(c_code)
                    
                # 2. Parse Lecturer
                l_name = get_val(r, 'lecturer')
                l_email = get_val(r, 'email')
                if not l_name:
                    l_name = "TBA Lecturer"
                else:
                    l_name = l_name.strip()
                    
                # 3. Parse Campus
                camp_name = canonical_campus_name(get_val(r, 'campus') or "Main Campus")
                if camp_name not in campuses:
                    campuses[camp_name] = {'name': camp_name}
                    
                # 4. Parse Program
                prog_name_raw = get_val(r, 'program')
                if not prog_name_raw:
                    # In flat timetable, if program is not given in columns, try to extract from sheet name
                    # e.g., "BSIT Y1S1 DAY" -> program "BSIT"
                    match = re.match(r'^([a-zA-Z\s]+)', sheet_name)
                    if match:
                        prog_name_raw = match.group(1).strip()
                    else:
                        prog_name_raw = sheet_name.strip()
                prog_name = canonical_program_name(prog_name_raw)
                    
                if prog_name not in programs:
                    programs[prog_name] = {'name': prog_name, 'campus': camp_name}
                    
                # 5. Student Group
                g_name = get_val(r, 'student_group')
                if not g_name:
                    # Construct from sheet name or program, appending trimester/semester/mode/option for uniqueness
                    g_parts = [sheet_name.strip()]
                    m_val = get_val(r, 'mode')
                    if m_val and str(m_val).strip() and str(m_val).strip().upper() not in sheet_name.upper():
                        g_parts.append(str(m_val).strip())
                    sem_val = get_val(r, 'semester')
                    if sem_val and str(sem_val).strip():
                        g_parts.append(str(sem_val).strip())
                    opt_val = get_val(r, 'option')
                    if opt_val and str(opt_val).strip():
                        g_parts.append(str(opt_val).strip())
                    g_name = " - ".join(g_parts)
                g_cap_s = get_val(r, 'capacity')
                try:
                    g_size = int(float(g_cap_s)) if g_cap_s else 40
                except ValueError:
                    g_size = 40
                    
                g_key = (prog_name, g_name)
                if g_key not in student_groups:
                    student_groups[g_key] = {'name': g_name, 'size': g_size, 'program': prog_name}
                    
                # 6. Lecturer Email Auto-Gen if missing
                if l_name != "TBA Lecturer" and not l_email:
                    l_email = generate_lecturer_email(l_name, uni_name)
                    warnings.append(f"Lecturer '{l_name}' has no email — auto-generating as '{l_email}'")
                elif l_name == "TBA Lecturer":
                    l_email = f"tba@{normalize_string(uni_name)}.edu"
                    
                if l_email not in lecturers:
                    lecturers[l_email] = {'name': l_name, 'email': l_email, 'department': 'Default Department'}
                    
                # 7. Rooms
                r_name = get_val(r, 'room')
                if r_name:
                    r_type_raw = get_val(r, 'room_type')
                    r_type = clean_room_type(r_name, r_type_raw)
                    r_cap_s = get_val(r, 'capacity')
                    try:
                        r_cap = int(float(r_cap_s)) if r_cap_s else 60
                    except ValueError:
                        r_cap = 60
                        
                    r_key = (camp_name, r_name)
                    if r_key not in rooms:
                        rooms[r_key] = {'name': r_name, 'capacity': r_cap, 'room_type': r_type, 'campus': camp_name}
                        
                # 8. Time slots
                day_raw = get_val(r, 'day')
                time_raw = get_val(r, 'time')
                day_num = parse_day(day_raw)
                start_t, end_t = parse_time_slot(time_raw)
                
                # Check for headers or titles repeated in data rows
                # (e.g. if the course code is exactly 'course' or 'code')
                if normalize_string(c_code) in ['course_code', 'code', 'unit_code']:
                    continue # Skip repeating header row
                    
                if day_num and start_t and end_t:
                    ts_key = (day_num, start_t, end_t)
                    if ts_key not in time_slots:
                        time_slots[ts_key] = {'day_of_week': day_num, 'start_time': start_t, 'end_time': end_t}
                        
                # 9. Course
                dur_raw = get_val(r, 'hours')
                try:
                    dur = int(float(dur_raw)) if dur_raw else 1
                except ValueError:
                    dur = 1
                    
                course_key = (prog_name, c_code, g_name)
                if course_key not in courses:
                    courses[course_key] = {
                        'code': c_code,
                        'name': c_name,
                        'duration_slots': dur,
                        'required_room_type': clean_room_type(r_name, get_val(r, 'room_type')),
                        'lecturer': l_email,
                        'student_group': g_name,
                        'program': prog_name
                    }
                    
        elif dtype == 'room':
            for r in data_rows:
                if not any(r):
                    continue
                nm = get_val(r, 'room') or get_val(r, 'room_name')
                if not nm:
                    continue
                nm = nm.strip()
                    
                # Skip header repetitions
                if normalize_string(nm) in ['room', 'room_name', 'name', 'venue']:
                    continue
                    
                cap_s = get_val(r, 'capacity')
                try:
                    cap = int(float(cap_s))
                except ValueError:
                    cap = 50
                    
                rt = get_val(r, 'room_type')
                camp_name = canonical_campus_name(get_val(r, 'campus') or "Main Campus")
                if camp_name not in campuses:
                    campuses[camp_name] = {'name': camp_name}
                    
                r_key = (camp_name, nm)
                rooms[r_key] = {
                    'name': nm,
                    'capacity': cap,
                    'room_type': clean_room_type(nm, rt),
                    'campus': camp_name
                }
                
        elif dtype == 'lecturer':
            for r in data_rows:
                if not any(r):
                    continue
                nm = get_val(r, 'lecturer') or get_val(r, 'lecturer_name')
                if not nm:
                    continue
                nm = nm.strip()
                if normalize_string(nm) in ['lecturer', 'lecturer_name', 'name', 'instructor']:
                    continue
                    
                em = get_val(r, 'email').lower()
                dept = get_val(r, 'department') or "Default Department"
                
                if not em:
                    em = generate_lecturer_email(nm, uni_name)
                    warnings.append(f"Lecturer '{nm}' has no email — auto-generating as '{em}'")
                    
                lecturers[em] = {
                    'name': nm,
                    'email': em,
                    'department': dept
                }
                
        elif dtype == 'student_group':
            for r in data_rows:
                if not any(r):
                    continue
                nm = get_val(r, 'student_group') or get_val(r, 'student_group_name')
                if not nm:
                    continue
                nm = nm.strip()
                if normalize_string(nm) in ['student_group', 'group', 'class', 'cohort']:
                    continue
                    
                sz_s = get_val(r, 'capacity') or get_val(r, 'size')
                try:
                    sz = int(float(sz_s))
                except ValueError:
                    sz = 40
                prog_name = canonical_program_name(get_val(r, 'program') or "Default Program")
                if prog_name not in programs:
                    programs[prog_name] = {'name': prog_name, 'campus': "Main Campus"}
                    
                g_key = (prog_name, nm)
                student_groups[g_key] = {
                    'name': nm,
                    'size': sz,
                    'program': prog_name
                }
                
        elif dtype == 'course':
            for r in data_rows:
                if not any(r):
                    continue
                code = normalize_course_code(get_val(r, 'course_code'))
                name = get_val(r, 'course_name')
                if name:
                    name = name.strip()
                if not code and not name:
                    continue
                if normalize_string(code) in ['course_code', 'code', 'unit_code']:
                    continue
                    
                if not code:
                    code = normalize_course_code(re.sub(r'\s+', '', name)[:8])
                if not name:
                    name = code
                
                code = normalize_course_code(code)
                    
                dur_s = get_val(r, 'hours')
                try:
                    dur = int(float(dur_s))
                except ValueError:
                    dur = 1
                    
                rt = get_val(r, 'room_type')
                lec_email = get_val(r, 'email').lower() or None
                grp_name = get_val(r, 'student_group') or None
                prog_name = canonical_program_name(get_val(r, 'program') or "Default Program")
                
                if prog_name not in programs:
                    programs[prog_name] = {'name': prog_name, 'campus': "Main Campus"}
                    
                grp_name_clean = grp_name.strip() if grp_name else ""
                course_key = (prog_name, code, grp_name_clean)
                courses[course_key] = {
                    'code': code,
                    'name': name,
                    'duration_slots': dur,
                    'required_room_type': clean_room_type('', rt) if rt else 'Lecture',
                    'lecturer': lec_email,
                    'student_group': grp_name,
                    'program': prog_name
                }

    # Deduplicate warning messages
    warnings = list(set(warnings))
    
    return {
        'campuses': list(campuses.values()),
        'programs': list(programs.values()),
        'lecturers': list(lecturers.values()),
        'rooms': list(rooms.values()),
        'student_groups': list(student_groups.values()),
        'courses': list(courses.values()),
        'time_slots': list(time_slots.values()),
        'warnings': warnings
    }

def import_entities(university, entities_dict) -> dict:
    """
    Creates/updates all entities in DB in correct dependency order within a transaction.
    """
    summary = {
        'campuses': 0,
        'programs': 0,
        'lecturers': 0,
        'rooms': 0,
        'student_groups': 0,
        'courses': 0,
        'time_slots': 0
    }
    
    with transaction.atomic():
        # Pre-fetch or create a default Faculty and Department under the university
        # to ensure any generated Program or Lecturer can be safely attached.
        default_campus = Campus.objects.filter(university=university).first()
        if not default_campus:
            default_campus = Campus.objects.create(university=university, name="Main Campus")
            
        default_faculty = Faculty.objects.filter(campus__university=university).first()
        if not default_faculty:
            default_faculty = Faculty.objects.create(campus=default_campus, name="Default Faculty")
            
        default_dept = Department.objects.filter(faculty__campus__university=university).first()
        if not default_dept:
            default_dept = Department.objects.create(faculty=default_faculty, name="Default Department")
            
        # Caches to avoid redundant DB reads/writes
        campus_cache = {c.name.strip().lower(): c for c in Campus.objects.filter(university=university)}
        program_cache = {p.name.strip().lower(): p for p in Program.objects.filter(department__faculty__campus__university=university)}
        dept_cache = {d.name.strip().lower(): d for d in Department.objects.filter(faculty__campus__university=university)}
        
        # 1. CAMPUSES
        new_campuses = []
        for c in entities_dict.get('campuses', []):
            name = c['name']
            name_lower = name.strip().lower()
            if name_lower not in campus_cache:
                campus_cache[name_lower] = Campus(university=university, name=name)
                new_campuses.append(campus_cache[name_lower])
        if new_campuses:
            Campus.objects.bulk_create(new_campuses)
            # Re-fetch to get IDs
            campus_cache = {c.name.strip().lower(): c for c in Campus.objects.filter(university=university)}
            summary['campuses'] = len(new_campuses)
            
        # 2. PROGRAMS
        new_programs = []
        for p in entities_dict.get('programs', []):
            name = p['name']
            name_lower = name.strip().lower()
            if name_lower not in program_cache:
                c_obj = campus_cache.get(p.get('campus', '').strip().lower(), default_campus)
                # Ensure we have department for program
                dept_name = "Dept of " + name
                dept_key = dept_name.strip().lower()
                if dept_key not in dept_cache:
                    # Check if there is an existing faculty under c_obj
                    fac = Faculty.objects.filter(campus=c_obj).first()
                    if not fac:
                        fac = Faculty.objects.create(campus=c_obj, name="Faculty of " + name)
                    dept_cache[dept_key] = Department.objects.create(faculty=fac, name=dept_name)
                    
                program_cache[name_lower] = Program(department=dept_cache[dept_key], name=name)
                new_programs.append(program_cache[name_lower])
        if new_programs:
            Program.objects.bulk_create(new_programs)
            program_cache = {p.name.strip().lower(): p for p in Program.objects.filter(department__faculty__campus__university=university)}
            summary['programs'] = len(new_programs)
            
        # 3. LECTURERS
        lecturer_cache = {l.email.strip().lower(): l for l in Lecturer.objects.filter(department__faculty__campus__university=university)}
        new_lecturers = []
        upd_lecturers = []
        for l in entities_dict.get('lecturers', []):
            email = l['email'].strip().lower()
            name = l['name']
            dept_name = l.get('department', 'Default Department')
            
            dept_key = dept_name.strip().lower()
            if dept_key not in dept_cache:
                dept_cache[dept_key] = Department.objects.create(faculty=default_faculty, name=dept_name)
                
            dept_obj = dept_cache[dept_key]
            
            if email in lecturer_cache:
                lec_obj = lecturer_cache[email]
                if lec_obj.name != name or lec_obj.department != dept_obj:
                    lec_obj.name = name
                    lec_obj.department = dept_obj
                    upd_lecturers.append(lec_obj)
            else:
                lecturer_cache[email] = Lecturer(name=name, email=email, department=dept_obj)
                new_lecturers.append(lecturer_cache[email])
                
        if new_lecturers:
            Lecturer.objects.bulk_create(new_lecturers)
            # Re-fetch
            lecturer_cache = {l.email.strip().lower(): l for l in Lecturer.objects.filter(department__faculty__campus__university=university)}
            summary['lecturers'] = len(new_lecturers)
        if upd_lecturers:
            Lecturer.objects.bulk_update(upd_lecturers, ['name', 'department'])
            
        # 4. ROOMS
        room_cache = {(r.campus_id, r.name.strip().lower()): r for r in Room.objects.filter(campus__university=university)}
        new_rooms = []
        upd_rooms = []
        for r in entities_dict.get('rooms', []):
            name = r['name'].strip()
            capacity = r['capacity']
            room_type = r['room_type']
            c_name = r.get('campus', 'Main Campus')
            c_obj = campus_cache.get(c_name.strip().lower(), default_campus)
            
            r_key = (c_obj.id, name.lower())
            if r_key in room_cache:
                room_obj = room_cache[r_key]
                if room_obj.capacity != capacity or room_obj.room_type != room_type:
                    room_obj.capacity = capacity
                    room_obj.room_type = room_type
                    upd_rooms.append(room_obj)
            else:
                room_cache[r_key] = Room(campus=c_obj, name=name, capacity=capacity, room_type=room_type)
                new_rooms.append(room_cache[r_key])
                
        if new_rooms:
            Room.objects.bulk_create(new_rooms)
            room_cache = {(r.campus_id, r.name.strip().lower()): r for r in Room.objects.filter(campus__university=university)}
            summary['rooms'] = len(new_rooms)
        if upd_rooms:
            Room.objects.bulk_update(upd_rooms, ['capacity', 'room_type'])
            
        # 5. STUDENT GROUPS
        group_cache = {(g.program_id, g.name.strip().lower()): g for g in StudentGroup.objects.filter(program__department__faculty__campus__university=university)}
        new_groups = []
        upd_groups = []
        for g in entities_dict.get('student_groups', []):
            name = g['name'].strip()
            size = g['size']
            prog_name = g.get('program', 'Default Program')
            prog_obj = program_cache.get(prog_name.strip().lower())
            if not prog_obj:
                continue
                
            g_key = (prog_obj.id, name.lower())
            if g_key in group_cache:
                g_obj = group_cache[g_key]
                if g_obj.size != size:
                    g_obj.size = size
                    upd_groups.append(g_obj)
            else:
                group_cache[g_key] = StudentGroup(program=prog_obj, name=name, size=size)
                new_groups.append(group_cache[g_key])
                
        if new_groups:
            StudentGroup.objects.bulk_create(new_groups)
            group_cache = {(g.program_id, g.name.strip().lower()): g for g in StudentGroup.objects.filter(program__department__faculty__campus__university=university)}
            summary['student_groups'] = len(new_groups)
        if upd_groups:
            StudentGroup.objects.bulk_update(upd_groups, ['size'])
            
        # 6. TIME SLOTS
        existing_ts = {(ts.day_of_week, ts.start_time, ts.end_time): ts for ts in TimeSlot.objects.filter(university=university)}
        new_ts = []
        
        # Determine slot numbers to assign to new time slots
        max_slot_by_day = {}
        for (day, _, _), ts in existing_ts.items():
            max_slot_by_day[day] = max(max_slot_by_day.get(day, 0), ts.slot_number)
            
        for ts in entities_dict.get('time_slots', []):
            day = ts['day_of_week']
            start = ts['start_time']
            end = ts['end_time']
            ts_key = (day, start, end)
            
            if ts_key not in existing_ts:
                slot_num = max_slot_by_day.get(day, 0) + 1
                max_slot_by_day[day] = slot_num
                
                # Check evening: starts at or after 17:00
                is_eve = start.hour >= 17
                
                new_ts_obj = TimeSlot(
                    university=university,
                    day_of_week=day,
                    start_time=start,
                    end_time=end,
                    slot_number=slot_num,
                    is_evening=is_eve
                )
                existing_ts[ts_key] = new_ts_obj
                new_ts.append(new_ts_obj)
                
        if new_ts:
            TimeSlot.objects.bulk_create(new_ts)
            summary['time_slots'] = len(new_ts)
            
        # 7. COURSES
        course_cache = {
            (c.program_id, c.code.strip().lower(), c.student_group_id): c 
            for c in Course.objects.filter(program__department__faculty__campus__university=university)
        }
        new_courses = []
        upd_courses = []
        for c in entities_dict.get('courses', []):
            code = normalize_course_code(c['code'])
            name = c['name']
            dur = c['duration_slots']
            room_type = c['required_room_type']
            prog_name = c['program']
            prog_obj = program_cache.get(prog_name.strip().lower())
            if not prog_obj:
                continue
                
            l_email = c.get('lecturer')
            lec_obj = lecturer_cache.get(l_email.strip().lower()) if (l_email and isinstance(l_email, str)) else None
            
            g_name = c.get('student_group')
            grp_obj = group_cache.get((prog_obj.id, g_name.strip().lower())) if (g_name and isinstance(g_name, str)) else None
            
            c_key = (prog_obj.id, code.lower(), grp_obj.id if grp_obj else None)
            if c_key in course_cache:
                c_obj = course_cache[c_key]
                if (c_obj.name != name or c_obj.duration_slots != dur or 
                    c_obj.required_room_type != room_type or 
                    c_obj.lecturer != lec_obj or c_obj.student_group != grp_obj):
                    c_obj.name = name
                    c_obj.duration_slots = dur
                    c_obj.required_room_type = room_type
                    c_obj.lecturer = lec_obj
                    c_obj.student_group = grp_obj
                    upd_courses.append(c_obj)
            else:
                course_cache[c_key] = Course(
                    program=prog_obj,
                    code=code,
                    name=name,
                    duration_slots=dur,
                    required_room_type=room_type,
                    lecturer=lec_obj,
                    student_group=grp_obj
                )
                new_courses.append(course_cache[c_key])
                
        if new_courses:
            Course.objects.bulk_create(new_courses)
            summary['courses'] = len(new_courses)
        if upd_courses:
            Course.objects.bulk_update(upd_courses, ['name', 'duration_slots', 'required_room_type', 'lecturer', 'student_group'])
            
    # Auto-heal and run auto-scheduling triggers if needed
    try:
        from scheduler.views import auto_heal_university_data
        auto_heal_university_data(university)
    except Exception:
        pass
        
    return summary
