import binascii
import docker
import hashlib
import os
import shutil
import time

from traceback import print_exc
from app import app
from app import db
from app import utils
from flask import request
from .models.program import Program
from .models.user import User


CODE_SUCCESS = 0
ERR_CODE_CONTAININT_FORBIDDEN_STRING = 1
ERR_CODE_COMPILATION_FAILED = 2
ERR_CODE_BIN_TOO_LARGE = 3
ERR_CODE_LINK_FAILED = 4
ERR_CODE_EXECUTION_FAILED = 5
ERR_CODE_EXECUTION_EXCEED_RAM_LIMIT = 6
ERR_CODE_EXECUTION_EXCEED_TIME_LIMIT = 7


def clean_programs_timeout_to_compile_or_test():
    for _ in range(5):
        try:
            utils.console(
                'Try to clean programs timeout to compile or test...'
            )
            Program.clean_programs_which_timeout_to_compile_or_test()
            db.session.commit()
            return True
        except:
            utils.console('Exception catched, trying again in 2sec')
            print_exc()
            time.sleep(2)

    utils.console('Could not clean programs which failed to compile or test')
    return False


@app.route('/compile_and_test', methods=['GET', 'POST'])
def compile_and_test():
    utils.console('Starting compile and test')

    if not clean_programs_timeout_to_compile_or_test():
        return ""

    client = docker.from_env()
    api_client = docker.APIClient(app.config['SOCK'])

    if utils.service_runs_already(
            client, app.config['NAME_OF_COMPILE_AND_TEST_SERVICE']):
        utils.console(
            'A program is currently being compiled or tested. Exiting.')
        return ""

    retry_count = 0
    while True:
        try:
            utils.console('Looking for a program to compile and test.')
            program_to_compile_and_test = Program.get_next_program_to_compile()
        except:
            retry_count += 1
            if retry_count < 5:
                utils.console('Exception catched, trying again in 2sec')
                time.sleep(2)
                continue
            else:
                utils.console(
                    'Could not look for a program to compile and test.')
                utils.console('Exception:')
                print_exc()
                return ""
        break

    if program_to_compile_and_test is None:
        utils.console('There is no program to compile and test. Exiting')
        return ""
    basename = os.path.splitext(program_to_compile_and_test.filename)[0]
    key_string = program_to_compile_and_test.key
    compiler = program_to_compile_and_test.compiler
    # Make sure the key can be converted in a 16-byte string
    try:
        key_bytes = bytes.fromhex(key_string)
        if len(key_bytes) != 16:
            raise
    except:
        utils.console("The key is invalid, setting the status to test failed.")
        program_to_compile_and_test.set_status_to_test_failed()
        db.session.commit()
        return ""

    utils.console(
        'Preparing to compile and test a program (basename=%s)' % basename)

    retry_count = 0
    while True:
        try:
            utils.console('Generating nonce')
            nonce = program_to_compile_and_test.generate_nonce()
            db.session.commit()
        except:
            retry_count += 1
            if retry_count < 5:
                utils.console('Exception catched, trying again in 2sec')
                time.sleep(2)
                continue
            else:
                utils.console('Could not generate nonce.')
                utils.console('Exception:')
                print_exc()
                return ""
        break

    # TODO: add more constraints on the service, use https instead of http, do not hardcode the urls
    restart_policy = docker.types.RestartPolicy(condition='on-failure',
                                                max_attempts=1)
    mem_limit = 2**20 * max(
        app.config['CHALLENGE_MAX_MEM_COMPILATION_IN_MB'],
        app.config['CHALLENGE_MAX_MEM_EXECUTION_IN_MB'])  # in Bytes
    resources = docker.types.Resources(mem_limit=mem_limit)
    networks = [app.config['COMPILE_AND_TEST_SERVICE_NETWORK']]
    env = ['UPLOAD_FOLDER=/uploads',
           'FILE_BASENAME=%s' % basename,
           'COMPILER=%s' % compiler,
           'URL_TO_PING_BACK=%s' % 'http://launcher:5000/compile_and_test_result/%s/%s/' % (
               basename, nonce),
           'URL_FOR_FETCHING_PLAINTEXTS=%s' % 'http://launcher:5000/get_plaintexts/%s/%s' % (
               basename, nonce),
           'CHALLENGE_MAX_MEM_COMPILATION_IN_MB=%d' % app.config[
               'CHALLENGE_MAX_MEM_COMPILATION_IN_MB'],
           'CHALLENGE_MAX_TIME_COMPILATION_IN_SECS=%d' % app.config[
               'CHALLENGE_MAX_TIME_COMPILATION_IN_SECS'],
           'CHALLENGE_MAX_BINARY_SIZE_IN_MB=%d' % app.config['CHALLENGE_MAX_BINARY_SIZE_IN_MB'],
           'CHALLENGE_MAX_MEM_EXECUTION_IN_MB=%d' % app.config['CHALLENGE_MAX_MEM_EXECUTION_IN_MB'],
           'CHALLENGE_MAX_TIME_EXECUTION_IN_SECS=%d' % app.config[
               'CHALLENGE_MAX_TIME_EXECUTION_IN_SECS'],
           'CHALLENGE_NUMBER_OF_TEST_VECTORS=%d' % app.config['CHALLENGE_NUMBER_OF_TEST_VECTORS'],
           ]

    # We copy the source file from /uploads to a fresh directory in /compilations
    dir_for_compilation = basename
    path_for_compilations = os.path.join('/compilations', dir_for_compilation)
    if not os.path.exists(path_for_compilations):
        os.makedirs(path_for_compilations)
    source_name = basename + '.c'
    path_to_uploaded_source = os.path.join('/uploads', source_name)
    path_to_source_for_compilation = os.path.join(
        path_for_compilations, source_name)
    if not os.path.exists(path_to_source_for_compilation):
        shutil.copy(path_to_uploaded_source, path_to_source_for_compilation)

    # We configure and launch the compile_and_test docker
    mounts = [
        '/whitebox_program_uploads/compilations/%s:/uploads:ro' % dir_for_compilation
    ]
    service = client.services.create(
        image='crx/compile_and_test',
        mounts=mounts,
        env=env,
        constraints=['node.labels.vm == node-sandbox'],
        name=app.config['NAME_OF_COMPILE_AND_TEST_SERVICE'],
        restart_policy=restart_policy,
        labels={'basename': str(basename)},
        networks=networks,
        resources=resources)

    while len(service.tasks()) == 0:
        time.sleep(0.1)
    task = service.tasks()[0]
    task_id = task['ID']
    retry_count = 0
    while True:
        try:
            utils.console('Setting the program\'s task id to %s' % task_id)
            program_to_compile_and_test.task_id = task_id
            db.session.commit()
        except:
            retry_count += 1
            if retry_count < 5:
                utils.console('Exception catched, trying again in 2sec')
                time.sleep(2)
                continue
            else:
                utils.console('Could not set the program task ide.')
                utils.console('Exception:')
                print_exc()
                return ""
        break

    utils.console('End of the compile_and_test procedure for the program:')
    utils.console(str(program_to_compile_and_test))

    return "youpi"


@app.route('/get_plaintexts/<basename:basename>/<basename:nonce>',
           methods=['GET'])
def get_plaintexts(basename, nonce):
    if not utils.basename_and_nonce_are_valid(basename, nonce):
        return ""

    path_to_plaintexts_file = os.path.join('/tmp', basename + '.plaintext.bin')

    # If it doesn't already exist, create the file containting the plaintexts
    if not os.path.exists(path_to_plaintexts_file):
        with open(path_to_plaintexts_file, 'wb') as f:
            f.write(os.urandom(
                16 * app.config['CHALLENGE_NUMBER_OF_TEST_VECTORS']))

    plaintexts = b''
    with open(path_to_plaintexts_file, 'rb') as f:
        plaintexts = f.read()

    return plaintexts


@app.route(
    '/compile_and_test_result/<basename:basename>/<basename:nonce>/<int:ret>',
    methods=['GET', 'POST'])
def compile_and_test_result(basename, nonce, ret):
    utils.console(
        "Entering compile_and_test_result(basename=%s, nonce=%s, ret=%d)" %
        (basename, nonce, ret))
    if not utils.basename_and_nonce_are_valid(basename, nonce) or ret is None:
        utils.console("Exception takes place ... (0)")
        return ""

    program = Program.get(basename)
    if program.status != Program.Status.submitted:
        utils.console(
            "The program %d status is %s. No need to proceed for this program."
            % (program._id, program.status)
        )
        utils.console("Exception takes place ... (1)")
        return ""

    # We (try to) remove the compilation directory
    dir_for_compilation = basename
    path_for_compilations = os.path.join('/compilations', dir_for_compilation)
    utils.console('Trying to remove %s' % str(path_for_compilations))
    try:
        shutil.rmtree(path_for_compilations)
    except:
        utils.console('Could NOT remove the directory %s' %
                      str(path_for_compilations))

    # We process the ret code
    if ret == ERR_CODE_CONTAININT_FORBIDDEN_STRING:
        postdata = request.get_json()
        program.set_status_to_preprocess_failed(postdata['error_message'])
    elif ret == ERR_CODE_COMPILATION_FAILED:
        postdata = request.get_json()
        if postdata:
            program.set_status_to_compilation_failed(postdata['error_message'])
        else:
            program.set_status_to_compilation_failed(
                'Compilation failed for unknown reason (may be due to an excessive memory usage).')
        utils.console(
            'Compilation failed for file with basename %s' % str(basename))
    elif ret == ERR_CODE_BIN_TOO_LARGE:
        program.set_status_to_compilation_failed(
            'Compiled binary file size exceeds the limit of %dMB.' % app.config['CHALLENGE_MAX_BINARY_SIZE_IN_MB'])
        utils.console(
            'Compilation failed for file with basename %s' % str(basename))
    elif ret == ERR_CODE_LINK_FAILED:
        program.set_status_to_link_failed()
        utils.console('Link failed for file with basename %s' % str(basename))
    elif ret == ERR_CODE_EXECUTION_EXCEED_RAM_LIMIT:
        postdata = request.get_json()
        program.set_status_to_execution_failed(
            "Execution reach memory limitation of %dMB. Memory consumption was %.2fMB." % (app.config['CHALLENGE_MAX_MEM_EXECUTION_IN_MB'], postdata['ram']))
        utils.console(
            'Code execution reach memory limit for file with basename %s' % str(basename))
    elif ret == ERR_CODE_EXECUTION_EXCEED_TIME_LIMIT:
        postdata = request.get_json()
        program.set_status_to_execution_failed(
            "Execution reach time limitation of %ds. Time used %.2fs" % (app.config['CHALLENGE_MAX_TIME_EXECUTION_IN_SECS'], postdata['cpu_time']))
        utils.console(
            'Code execution reach time limit for file with basename %s' % str(basename))
    elif ret == ERR_CODE_EXECUTION_FAILED:
        program.set_status_to_execution_failed()
        utils.console(
            'Code execution failed for file with basename %s' % str(basename))
    elif ret == CODE_SUCCESS:
        utils.console('Success for file with basename %s' % str(basename))
    else:
        utils.console(
            "We received an unexpected return code (%s) for file with basename %s" % (str(ret), str(basename)))
    db.session.commit()

    if ret != ERR_CODE_EXECUTION_FAILED:
        client = docker.from_env()
        utils.remove_compiler_service_for_basename(client, basename, app)

    if ret != CODE_SUCCESS:
        utils.console("Failed to compiling ... ")
        return ""

    # If we reach this point, the program was successfuly compiled,
    # we can test the ciphertexts
    response = request.get_json()
    size_factor = response['size_factor']
    ram_factor = response['ram_factor']
    time_factor = response['time_factor']
    program.set_performance_factor(size_factor, ram_factor, time_factor)

    ciphertexts = bytes.fromhex(response['ciphertexts'])
    number_of_test_vectors = app.config['CHALLENGE_NUMBER_OF_TEST_VECTORS']
    if len(ciphertexts) != 16 * number_of_test_vectors:
        utils.console("The length of the ciphertexts is %d, we were expecting %d." % (
            len(ciphertexts), 16 * number_of_test_vectors))
        error_message = "The stream of ciphertexts does not have the appropriate length."
        utils.console(error_message)
        program.error_message = error_message
        program.set_status_to_test_failed()
        db.session.commit()

        utils.console("Exception take place... (3)")
        return ""

    # If we reach this point, the ciphertexts stream has the appropriate length
    utils.console("We received the appropriate number of ciphertexts.")
    utils.console(
        "Testing the plaintexts against the ciphertexts using the announced key...")

    # Retrieve the plaintexts from the saved file
    path_to_plaintexts_file = os.path.join('/tmp', basename + '.plaintext.bin')
    plaintexts = b''
    with open(path_to_plaintexts_file, 'rb') as f:
        plaintexts = f.read()

    # Check the ciphertexts against the plaintext and key.
    # TODO the db should always return the key as 16 bytes
    key = bytes.fromhex(program.key)
    try:
        expected_ciphertexts = utils.compute_ciphertexts(
            plaintexts, key, number_of_test_vectors)
        if len(expected_ciphertexts) != 16 * number_of_test_vectors:
            raise
    except:
        error_message = "Could not compute the test vectors for the given key."
        program.set_status_to_test_failed(error_message)
        db.session.commit()
        return ""
    for i in range(number_of_test_vectors):
        ciphertext = ciphertexts[16*i:16*(i+1)]
        expected_ciphertext = expected_ciphertexts[16*i:16*(i+1)]
        if ciphertext != expected_ciphertext:
            plaintext = plaintexts[16*i:16*(i+1)]
            utils.console("One of the ciphertext failed the test (plaintext=%s, key=%s, ciphertext=%s)." % (
                plaintext, key, ciphertext))
            error_message = '''One of the tests failed:
- plaintext   %s
- key         %s
- ciphertext  %s
- expected    %s''' % (binascii.hexlify(plaintext).decode(),
                       binascii.hexlify(key).decode(),
                       binascii.hexlify(ciphertext).decode(),
                       binascii.hexlify(expected_ciphertext).decode())
            program.set_status_to_test_failed(error_message)
            db.session.commit()
            return ""

    # If we reach this point, all the tests were successful.
    # We save 10 test vectors for key validation in the database:
    plaintexts_for_breaking = plaintexts[0:10*16]
    ciphertexts_for_breaking = ciphertexts[0:10*16]
    program.plaintexts = plaintexts_for_breaking
    program.ciphertexts = ciphertexts_for_breaking
    # we save one pair for validating inversion
    plaintext_for_inverting = plaintexts[10*16:11*16]
    ciphertext_for_inverting = ciphertexts[10*16:11*16]
    program.plaintext_sha256_for_inverting = hashlib.sha256(
        plaintext_for_inverting).hexdigest()
    program.ciphertext_for_inverting = ciphertext_for_inverting

    program.set_status_to_unbroken()
    db.session.commit()
    utils.console("The program is unbroken!")

    # Cleanup
    try:
        os.remove(path_to_plaintexts_file)
        utils.console("We removed the file %s" % path_to_plaintexts_file)
    except:
        utils.console("Could NOT remove the file %s" % path_to_plaintexts_file)

    return ""
